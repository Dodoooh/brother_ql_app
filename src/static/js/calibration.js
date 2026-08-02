// Brother QL Printer App - Print alignment calibration
//
// Content can land slightly off-centre on the physical label, or come out a
// touch larger or smaller than nominal: die-cut registration tolerance,
// per-model raster offsets and media variation all add up. This module drives
// the "print a target, see how far it is out, nudge, print again" loop and
// stores the resulting correction per label type.
//
// Storage lives in the normal settings document under a top-level map keyed by
// label identifier:
//
//     "calibration": { "d24": { "x_mm": -0.5, "y_mm": 1.0, "scale": 0.98 } }
//
// Sign convention: x_mm > 0 moves the printed content to the right, y_mm > 0
// moves it down (later in the feed direction). `scale` is a size correction
// around 1.0 for printers that lay ink down slightly larger or smaller than
// nominal: 0.98 prints two percent smaller. Absent or 1.0 means no size
// correction.
//
// The user is never asked to reason about signs or multipliers: the offset is
// nudged by direction, the size in percent, and the raw numbers are only there
// to be read back and fine-tuned.
//
// All three values are applied WHEN PRINTING ONLY. Previews always show the
// label as it was designed — calibration exists so the physical label ends up
// matching the preview, not the other way round. That matters most for `scale`,
// which is a printer correction and not a way to shrink a design: nothing in
// the preview moves with it.

// Largest offset in either direction. Mirrors the server's own limit, so a
// value the pad or the number fields can produce is never rejected on save.
const CALIBRATION_LIMIT_MM = 10;

// Largest size correction, as a percentage away from nominal. A printer that is
// out by more than a few percent has a different problem, so the control is
// deliberately built for fine adjustment rather than for zooming. Mirrors the
// server's own clamp.
const CALIBRATION_SCALE_LIMIT_PERCENT = 5;

// Step preselected when the dialog opens. The steps on offer are the
// data-cal-step buttons in the dialog; this must be one of them.
const CALIBRATION_DEFAULT_STEP = 0.5;

// Same, for the size correction, in percentage points (data-cal-scale-step).
const CALIBRATION_DEFAULT_SCALE_STEP = 0.5;

// The calibration map exactly as last read from the API, keyed by label
// identifier. Kept so the Settings summary can render without a round trip.
let calibrationMap = {};

// ---- State of the currently open dialog ----

// Label identifier being calibrated (taken from #label-size when opening).
let calibrationLabelSize = '';

// Working value, not yet saved.
let calibrationDraft = { x_mm: 0, y_mm: 0, scale: 1 };

// The stored value for this medium at the moment the dialog was opened, or
// null when the medium had no calibration. Shown so the user can always see
// what is actually in effect.
let calibrationSaved = null;

// The value the most recent test label was printed with, or null when none was
// printed in this session. Shown so the user can tell whether the last nudge
// moved things closer or overshot.
let calibrationLastPrinted = null;

// Currently selected nudge step in mm.
let calibrationStep = CALIBRATION_DEFAULT_STEP;

// Currently selected size step, in percentage points.
let calibrationScaleStep = CALIBRATION_DEFAULT_SCALE_STEP;

// Bootstrap modal instance, created lazily on first open.
let calibrationModal = null;

// Aborts a superseded target-preview request.
let calibrationPreviewController = null;

/**
 * Round a millimetre value to two decimals and normalise negative zero, so
 * "-0" never reaches the UI or the API.
 * @param {number} value
 * @returns {number}
 */
function roundMm(value) {
    const rounded = Math.round(value * 100) / 100;
    return Object.is(rounded, -0) ? 0 : rounded;
}

/**
 * Clamp a millimetre value into the allowed range, rounding it and turning
 * anything non-numeric into 0.
 * @param {*} value
 * @returns {number}
 */
function clampMm(value) {
    const number = typeof value === 'number' ? value : parseFloat(value);
    if (!Number.isFinite(number)) return 0;
    return roundMm(Math.min(CALIBRATION_LIMIT_MM, Math.max(-CALIBRATION_LIMIT_MM, number)));
}

/**
 * Format a millimetre value for display: at least one decimal, never a
 * trailing pair of zeros ("0.50" -> "0.5", "1" -> "1.0", "0.25" -> "0.25").
 * @param {number} value
 * @returns {string}
 */
function formatMm(value) {
    const text = roundMm(value).toFixed(2);
    return text.endsWith('0') ? text.slice(0, -1) : text;
}

/**
 * Round a scale factor to four decimals, which is one decimal of a percent —
 * finer than that is noise on a 300 dpi printer.
 * @param {number} value
 * @returns {number}
 */
function roundScale(value) {
    return Math.round(value * 10000) / 10000;
}

/**
 * Clamp a scale factor into the allowed range around 1.0. Anything unusable
 * (missing, non-numeric, zero, negative) becomes 1.0, i.e. no correction — an
 * old settings file without a scale is simply an uncorrected one.
 * @param {*} value
 * @returns {number}
 */
function clampScale(value) {
    const number = typeof value === 'number' ? value : parseFloat(value);
    if (!Number.isFinite(number) || number <= 0) return 1;
    const limit = CALIBRATION_SCALE_LIMIT_PERCENT / 100;
    return roundScale(Math.min(1 + limit, Math.max(1 - limit, number)));
}

/**
 * The scale factor a percentage away from nominal describes: 2 -> 1.02.
 * @param {*} percent - percentage points, positive for larger
 * @returns {number} the clamped scale factor
 */
function scaleFromPercent(percent) {
    const number = typeof percent === 'number' ? percent : parseFloat(percent);
    if (!Number.isFinite(number)) return 1;
    return clampScale(1 + number / 100);
}

/**
 * How far a scale factor is away from nominal, in percentage points: 0.98 ->
 * -2. This is what the user is shown and what every step operates on, so no one
 * has to reason about a multiplier.
 * @param {*} scale
 * @returns {number}
 */
function percentFromScale(scale) {
    const percent = Math.round((clampScale(scale) - 1) * 10000) / 100;
    return Object.is(percent, -0) ? 0 : percent;
}

/**
 * Format a percentage compactly, without trailing zeros ("2", "0.5", "1.25").
 * @param {number} value
 * @returns {string}
 */
function formatPercent(value) {
    const rounded = Math.round(value * 100) / 100;
    return String(Object.is(rounded, -0) ? 0 : rounded);
}

/**
 * Format a scale factor as the raw multiplier that is actually stored, so the
 * percentage on screen can always be checked against it ("0.980", "1.0025").
 * @param {number} value
 * @returns {string}
 */
function formatScale(value) {
    const text = roundScale(value).toFixed(4);
    return text.endsWith('0') ? text.slice(0, -1) : text;
}

/**
 * Normalise anything that claims to be a calibration into
 * {x_mm, y_mm, scale}.
 * @param {*} calibration - a stored/entered calibration, possibly malformed
 * @returns {{x_mm: number, y_mm: number, scale: number}}
 */
function normaliseCalibration(calibration) {
    const source = calibration && typeof calibration === 'object' ? calibration : {};
    return {
        x_mm: clampMm(source.x_mm),
        y_mm: clampMm(source.y_mm),
        // Absent is the common case: every calibration stored before the size
        // correction existed, and every medium that only needs a shift.
        scale: source.scale === undefined || source.scale === null ? 1 : clampScale(source.scale)
    };
}

/**
 * A calibration that corrects nothing. Fresh object every time, because the
 * draft is mutated in place.
 * @returns {{x_mm: number, y_mm: number, scale: number}}
 */
function neutralCalibration() {
    return { x_mm: 0, y_mm: 0, scale: 1 };
}

/**
 * The wire form of a calibration: the offset always, the scale only when it
 * actually corrects something.
 *
 * Absent and 1.0 mean the same thing to the server, and leaving the field out
 * keeps a plain offset byte-identical to what this app has always sent — a
 * server that predates the size correction therefore keeps working for everyone
 * who does not use one.
 *
 * @param {*} calibration
 * @returns {{x_mm: number, y_mm: number, scale: (number|undefined)}}
 */
function serialiseCalibration(calibration) {
    const value = normaliseCalibration(calibration);
    const wire = { x_mm: value.x_mm, y_mm: value.y_mm };
    if (value.scale !== 1) wire.scale = value.scale;
    return wire;
}

/**
 * True when a calibration changes nothing about the print.
 * @param {*} calibration
 * @returns {boolean}
 */
function calibrationIsNeutral(calibration) {
    const value = normaliseCalibration(calibration);
    return value.x_mm === 0 && value.y_mm === 0 && value.scale === 1;
}

/**
 * True when a calibration shifts nothing (it may still resize).
 * @param {*} calibration
 * @returns {boolean}
 */
function calibrationHasNoOffset(calibration) {
    const value = normaliseCalibration(calibration);
    return value.x_mm === 0 && value.y_mm === 0;
}

/**
 * True when two calibrations describe the same correction.
 * @param {*} a
 * @param {*} b
 * @returns {boolean}
 */
function calibrationsEqual(a, b) {
    if (!a || !b) return !a && !b;
    const left = normaliseCalibration(a);
    const right = normaliseCalibration(b);
    return left.x_mm === right.x_mm && left.y_mm === right.y_mm && left.scale === right.scale;
}

/**
 * Describe one axis of an offset in words, so nobody has to work out what the
 * sign means: "0.5 mm right", "1 mm up", "centred".
 * @param {number} value - the axis value in mm
 * @param {string} axis - 'x' (left/right) or 'y' (up/down)
 * @returns {string}
 */
function describeCalibrationAxis(value, axis) {
    const mm = roundMm(value);
    if (mm === 0) return 'centred';
    const direction = axis === 'x'
        ? (mm > 0 ? 'right' : 'left')
        : (mm > 0 ? 'down' : 'up');
    return `${formatMm(Math.abs(mm))} mm ${direction}`;
}

/**
 * Describe the shift of a calibration in words, e.g. "0.5 mm right, 1 mm down".
 * @param {*} calibration
 * @returns {string} the description, or "no shift" when nothing moves
 */
function describeCalibrationOffset(calibration) {
    const value = normaliseCalibration(calibration);
    if (calibrationHasNoOffset(value)) return 'no shift';

    const parts = [];
    if (value.x_mm !== 0) parts.push(describeCalibrationAxis(value.x_mm, 'x'));
    if (value.y_mm !== 0) parts.push(describeCalibrationAxis(value.y_mm, 'y'));
    return parts.join(', ');
}

/**
 * Describe a size correction in words, so a multiplier never has to be read as
 * one: 0.98 -> "2 % smaller".
 * @param {number} scale
 * @returns {string} the description, or "same size" at 1.0
 */
function describeCalibrationScale(scale) {
    const percent = percentFromScale(scale);
    if (percent === 0) return 'same size';
    return `${formatPercent(Math.abs(percent))} % ${percent < 0 ? 'smaller' : 'larger'}`;
}

/**
 * Describe a whole calibration in one line, e.g. "0.5 mm right, 1 mm down,
 * 2 % smaller". Used everywhere a stored value is listed.
 * @param {*} calibration
 * @returns {string} the description, or "no correction" when nothing changes
 */
function describeCalibration(calibration) {
    const value = normaliseCalibration(calibration);
    if (calibrationIsNeutral(value)) return 'no correction';

    const parts = [];
    if (!calibrationHasNoOffset(value)) parts.push(describeCalibrationOffset(value));
    if (value.scale !== 1) parts.push(describeCalibrationScale(value.scale));
    return parts.join(', ');
}

/**
 * Describe a calibration as an instruction to the printer, which is how the
 * dialog's live readout and the confirmation messages phrase it: "Shift the
 * print 0.5 mm right and print 2 % smaller".
 * @param {*} calibration
 * @param {boolean} [capitalised] - start the sentence with a capital
 * @returns {string} the instruction, or an empty string when nothing changes
 */
function describeCalibrationAction(calibration, capitalised = true) {
    const value = normaliseCalibration(calibration);
    const parts = [];
    if (!calibrationHasNoOffset(value)) {
        parts.push(`${capitalised && parts.length === 0 ? 'Shift' : 'shift'} the print ` +
            describeCalibrationOffset(value));
    }
    if (value.scale !== 1) {
        const lead = capitalised && parts.length === 0 ? 'Print' : 'print';
        parts.push(`${lead} it ${describeCalibrationScale(value.scale)}`);
    }
    return parts.join(' and ');
}

/**
 * Human name of a label type, from the media catalogue when it knows the
 * identifier and from the identifier itself otherwise.
 * @param {string} identifier - e.g. "d24"
 * @returns {string}
 */
function calibrationMediumName(identifier) {
    const entry = typeof labelEntryFor === 'function' ? labelEntryFor(identifier) : null;
    if (entry && entry.name) return entry.name;
    return identifier ? `Label type ${identifier}` : 'No label type selected';
}

/**
 * Store the calibration map read from the API and refresh the Settings
 * summary. Called from loadSettings().
 * @param {*} map - the settings document's `calibration` value (may be absent)
 */
function setCalibrationMap(map) {
    calibrationMap = {};
    if (map && typeof map === 'object') {
        Object.keys(map).forEach(identifier => {
            const value = normaliseCalibration(map[identifier]);
            // A stored zero offset with a scale of 1 is the same as none at
            // all; drop it so the summary only lists media that are corrected.
            if (!calibrationIsNeutral(value)) calibrationMap[identifier] = value;
        });
    }
    renderCalibrationSummary();
}

/**
 * The stored calibration of a label type, or null when it has none.
 * @param {string} identifier
 * @returns {?{x_mm: number, y_mm: number, scale: number}}
 */
function storedCalibrationFor(identifier) {
    const value = calibrationMap[identifier];
    return value ? normaliseCalibration(value) : null;
}

/**
 * Render the "Print Alignment" summary in Settings: one row per calibrated
 * label type plus the button that opens the dialog for the selected medium.
 * Makes it obvious at a glance which media are calibrated and which are not.
 */
function renderCalibrationSummary() {
    const list = document.getElementById('calibration-list');
    const openLabel = document.getElementById('calibration-open-label');
    const labelSizeEl = document.getElementById('label-size');
    const current = labelSizeEl ? labelSizeEl.value : '';

    if (openLabel) {
        openLabel.textContent = current
            ? `${calibrationMediumName(current)} (${current})`
            : 'the selected label type';
    }

    if (!list) return;

    const identifiers = Object.keys(calibrationMap).sort();

    if (identifiers.length === 0) {
        list.innerHTML =
            '<div class="cal-empty">' +
            '<i class="bi bi-crosshair" aria-hidden="true"></i>' +
            '<span>No label type is calibrated. Every medium prints exactly as previewed.</span>' +
            '</div>';
        return;
    }

    list.innerHTML = identifiers.map(identifier => {
        const value = calibrationMap[identifier];
        const isCurrent = identifier === current;
        return (
            `<div class="cal-item${isCurrent ? ' is-current' : ''}">` +
                `<button type="button" class="cal-item-main" data-cal-edit="${escapeHtml(identifier)}" ` +
                        `title="Adjust the alignment of ${escapeHtml(calibrationMediumName(identifier))}">` +
                    '<span class="cal-item-head">' +
                        `<span class="cal-item-name">${escapeHtml(calibrationMediumName(identifier))}</span>` +
                        `<span class="cal-item-id mono">${escapeHtml(identifier)}</span>` +
                        (isCurrent ? '<span class="cal-item-current">loaded</span>' : '') +
                    '</span>' +
                    `<span class="cal-item-value mono">${escapeHtml(describeCalibration(value))}</span>` +
                '</button>' +
                `<button type="button" class="cal-item-clear" data-cal-clear="${escapeHtml(identifier)}" ` +
                        `aria-label="Remove the calibration of ${escapeHtml(identifier)}" ` +
                        'title="Reset this label type to no correction">' +
                    '<i class="bi bi-x-lg" aria-hidden="true"></i>' +
                '</button>' +
            '</div>'
        );
    }).join('');
}

/**
 * Read the settings document straight from the API. Used before every write so
 * a calibration save never clobbers changes made elsewhere.
 * @returns {Promise<?Object>} the settings, or null when unreadable
 */
async function fetchSettingsDocument() {
    try {
        const response = await fetch('/api/v1/settings');
        if (!response.ok) return null;
        return await response.json();
    } catch (error) {
        console.error('Error reading settings for calibration:', error);
        return null;
    }
}

/**
 * Write one label type's calibration back to the settings document.
 *
 * Reads the current settings first and merges into their calibration map, so
 * other media keep their values even when they were changed in another tab.
 * Only the three settings the API requires are sent alongside, which means an
 * unsaved edit in the settings form is never committed as a side effect.
 *
 * @param {string} identifier - label type to write
 * @param {?{x_mm: number, y_mm: number, scale: number}} calibration - the
 *     calibration to store, or null to remove it
 * @returns {Promise<boolean>} true when the settings were written
 */
async function persistCalibration(identifier, calibration) {
    if (!identifier) return false;

    const base = await fetchSettingsDocument();
    const map = {};

    // Start from the freshly read map when we have one, otherwise from the
    // copy we already hold, so a failed read cannot silently drop other media.
    const source = (base && base.calibration && typeof base.calibration === 'object')
        ? base.calibration
        : calibrationMap;
    Object.keys(source).forEach(key => {
        const value = normaliseCalibration(source[key]);
        if (!calibrationIsNeutral(value)) map[key] = serialiseCalibration(value);
    });

    if (calibration && !calibrationIsNeutral(calibration)) {
        map[identifier] = serialiseCalibration(calibration);
    } else {
        delete map[identifier];
    }

    const body = {
        printer_uri: (base && base.printer_uri) || document.getElementById('printer-uri').value,
        printer_model: (base && base.printer_model) || document.getElementById('printer-model').value,
        label_size: (base && base.label_size) || document.getElementById('label-size').value,
        calibration: map
    };

    const response = await fetch('/api/v1/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
    });

    if (!response.ok) {
        let message = `Error: ${response.status}`;
        try {
            const data = await response.json();
            // Schema rejections from the request validator carry an empty
            // message and put the reason in `details`.
            message = data.message || data.details || message;
        } catch (e) {
            // Non-JSON body: keep the generic message.
        }
        // A server that predates the size correction rejects the extra field
        // rather than ignoring it. Say so plainly instead of showing a schema
        // complaint, and point at the way out.
        if (response.status === 400 && Object.keys(map).some(key => map[key].scale !== undefined) &&
                /scale|additional/i.test(message)) {
            throw new Error('This server build does not accept a size correction yet. ' +
                'Clear the size correction to save the offset on its own, or update the server.');
        }
        throw new Error(message);
    }

    calibrationMap = map;
    renderCalibrationSummary();
    return true;
}

/**
 * Build the request body for the calibration endpoints. Mirrors the shape the
 * other print endpoints use (content + a `settings` block), with the label type
 * and the calibration under test spelled out.
 *
 * The calibration is carried twice on purpose. `settings.calibration` is the map
 * the render path resolves it from, keyed by label identifier exactly as it is
 * stored — sending it there is what lets a value be tried before it is saved.
 * The top-level `calibration` states the same correction as a plain
 * {x_mm, y_mm} plus `scale` when there is one, which is the direct reading of
 * "print the target with this correction". An empty map means "as designed",
 * which is what a target preview asks for: the preview never carries a
 * calibration.
 *
 * @param {string} identifier - label type to print the target for
 * @param {{x_mm: number, y_mm: number, scale: number}} calibration - the
 *     correction to apply to this print
 * @returns {Object}
 */
function buildCalibrationRequest(identifier, calibration) {
    const base = typeof collectPreviewSettings === 'function'
        ? collectPreviewSettings()
        : {};
    const value = normaliseCalibration(calibration);
    const map = {};
    if (!calibrationIsNeutral(value)) map[identifier] = serialiseCalibration(value);

    return {
        label_size: identifier,
        calibration: serialiseCalibration(value),
        settings: Object.assign({}, base, {
            label_size: identifier,
            // A calibration target is always a single label: printing ten of
            // them would only waste media.
            copies: 1,
            calibration: map
        })
    };
}

/**
 * POST to a calibration endpoint and normalise the outcome, including the case
 * where the server does not know the endpoint yet.
 * @param {string} url - endpoint path
 * @param {Object} body - JSON request body
 * @param {?AbortSignal} signal - optional abort signal
 * @returns {Promise<{ok: boolean, status: number, data: Object, missing: boolean}>}
 */
async function postCalibration(url, body, signal) {
    const options = {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
    };
    if (signal) options.signal = signal;

    const response = await fetch(url, options);
    let data = {};
    try {
        data = await response.json();
    } catch (e) {
        // Empty or non-JSON body (e.g. an HTML 404 page): keep {}.
    }

    return {
        ok: response.ok,
        status: response.status,
        data: data || {},
        // 404/405/501 all mean "this build of the server has no calibration
        // endpoints", which is a different problem from a rejected request.
        missing: response.status === 404 || response.status === 405 || response.status === 501
    };
}

/**
 * Message for a failed calibration request, telling "the server cannot do this
 * yet" apart from "the request was wrong".
 * @param {{status: number, data: Object, missing: boolean}} result
 * @returns {string}
 */
function calibrationErrorMessage(result) {
    if (result.missing) {
        return 'This server build has no calibration endpoints yet. The calibration can still be saved and will take effect once the server supports it.';
    }
    // A schema rejection carries an empty message and its reason in `details`.
    const message = result.data.message || result.data.details || `Error: ${result.status}`;
    // Same story as on save: a server that predates the size correction turns
    // it down instead of ignoring it.
    if (result.status === 400 && calibrationDraft.scale !== 1 && /scale|additional/i.test(message)) {
        return 'This server build does not know the size correction yet. Clear it to print a target with the offset alone.';
    }
    return message;
}

/**
 * Show a status line inside the dialog.
 * @param {string} message - text to show (empty hides the line)
 * @param {string} [kind] - 'info' | 'success' | 'warning' | 'error'
 */
function setCalibrationStatus(message, kind = 'info') {
    const el = document.getElementById('cal-status');
    if (!el) return;

    el.textContent = '';
    el.className = `cal-status cal-status--${kind}` + (message ? '' : ' d-none');
    if (!message) return;

    // The line carries no colour of its own any more, so the severity has to be
    // legible without one. Same icon vocabulary as the notifications, so "this
    // failed" looks the same wherever the app says it.
    const icon = document.createElement('i');
    icon.className = (typeof getNotificationIcon === 'function'
        ? getNotificationIcon(kind)
        : 'bi bi-info-circle-fill') + ' cal-status-icon';
    icon.setAttribute('aria-hidden', 'true');
    el.appendChild(icon);

    const text = document.createElement('span');
    text.textContent = message;
    el.appendChild(text);
}

/**
 * Build the live readout for the working draft: one sentence in the printer's
 * terms, with the values themselves emphasised. Deliberately phrased as an
 * instruction to the printer ("shift", "print smaller") and never as a change to
 * the design, because none of it shows up in the preview.
 * @returns {string} HTML for #cal-readout
 */
function calibrationReadoutHtml() {
    if (calibrationIsNeutral(calibrationDraft)) {
        return '<span class="cal-readout-zero">Printed exactly as previewed — no shift, no size correction</span>';
    }

    const value = normaliseCalibration(calibrationDraft);
    const parts = [];
    if (!calibrationHasNoOffset(value)) {
        parts.push(`Shift the print <strong>${escapeHtml(describeCalibrationOffset(value))}</strong>`);
    }
    if (value.scale !== 1) {
        const lead = parts.length === 0 ? 'Print' : 'print';
        parts.push(`${lead} it <strong>${escapeHtml(describeCalibrationScale(value.scale))}</strong>`);
    }
    return parts.join(' and ');
}

/**
 * Push the current draft into every control that shows it: the readout, the
 * three number inputs, the size factor and the "saved / last printed"
 * comparison.
 */
function renderCalibrationDraft() {
    const xInput = document.getElementById('cal-x');
    const yInput = document.getElementById('cal-y');
    const scaleInput = document.getElementById('cal-scale');
    const scaleFactor = document.getElementById('cal-scale-factor');
    const scaleReset = document.getElementById('cal-scale-reset');
    const readout = document.getElementById('cal-readout');
    const savedEl = document.getElementById('cal-saved-value');
    const printedEl = document.getElementById('cal-printed-value');
    const printedRow = document.getElementById('cal-printed-row');
    const removeBtn = document.getElementById('cal-remove');
    const saveBtn = document.getElementById('cal-save');

    // Do not fight the user while they are typing into a field.
    if (xInput && document.activeElement !== xInput) xInput.value = formatMm(calibrationDraft.x_mm);
    if (yInput && document.activeElement !== yInput) yInput.value = formatMm(calibrationDraft.y_mm);
    if (scaleInput && document.activeElement !== scaleInput) {
        scaleInput.value = formatPercent(percentFromScale(calibrationDraft.scale));
    }

    // The multiplier that is actually stored, next to the percentage the user
    // works in — so "0.5 %" can always be checked against "x1.005".
    if (scaleFactor) {
        scaleFactor.textContent = `×${formatScale(calibrationDraft.scale)}`;
        scaleFactor.classList.toggle('is-set', calibrationDraft.scale !== 1);
    }
    if (scaleReset) {
        scaleReset.disabled = calibrationDraft.scale === 1;
    }

    if (readout) {
        readout.innerHTML = calibrationReadoutHtml();
    }

    if (savedEl) {
        savedEl.textContent = calibrationSaved
            ? describeCalibration(calibrationSaved)
            : 'none stored';
    }

    if (printedRow && printedEl) {
        if (calibrationLastPrinted) {
            printedRow.classList.remove('d-none');
            const same = calibrationsEqual(calibrationLastPrinted, calibrationDraft);
            printedEl.textContent = describeCalibration(calibrationLastPrinted) +
                (same ? ' (unchanged since)' : ' — adjusted since');
        } else {
            printedRow.classList.add('d-none');
        }
    }

    if (removeBtn) {
        removeBtn.disabled = !calibrationSaved;
    }
    if (saveBtn) {
        saveBtn.disabled = calibrationsEqual(calibrationSaved || neutralCalibration(), calibrationDraft);
    }
}

/**
 * Reflect the selected steps in the step buttons of both groups.
 */
function renderCalibrationStep() {
    document.querySelectorAll('[data-cal-step]').forEach(button => {
        const active = parseFloat(button.getAttribute('data-cal-step')) === calibrationStep;
        button.classList.toggle('is-active', active);
        button.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
    document.querySelectorAll('[data-cal-scale-step]').forEach(button => {
        const active = parseFloat(button.getAttribute('data-cal-scale-step')) === calibrationScaleStep;
        button.classList.toggle('is-active', active);
        button.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
}

/**
 * Apply one nudge in a compass direction and refresh the controls. This is the
 * primary way to change the offset: the user picks a direction, never a sign.
 * @param {string} direction - 'left' | 'right' | 'up' | 'down'
 */
function nudgeCalibration(direction) {
    if (direction === 'left') {
        calibrationDraft.x_mm = clampMm(calibrationDraft.x_mm - calibrationStep);
    } else if (direction === 'right') {
        calibrationDraft.x_mm = clampMm(calibrationDraft.x_mm + calibrationStep);
    } else if (direction === 'up') {
        calibrationDraft.y_mm = clampMm(calibrationDraft.y_mm - calibrationStep);
    } else if (direction === 'down') {
        calibrationDraft.y_mm = clampMm(calibrationDraft.y_mm + calibrationStep);
    } else {
        return;
    }
    renderCalibrationDraft();
}

/**
 * Apply one size step and refresh the controls. The step is added in percent
 * rather than multiplied onto the factor, so repeated nudges land on round
 * values (0.5 %, 1 %, 1.5 %) instead of drifting.
 * @param {string} direction - 'smaller' | 'larger'
 */
function nudgeCalibrationScale(direction) {
    const step = direction === 'smaller'
        ? -calibrationScaleStep
        : (direction === 'larger' ? calibrationScaleStep : null);
    if (step === null) return;

    calibrationDraft.scale = scaleFromPercent(percentFromScale(calibrationDraft.scale) + step);
    renderCalibrationDraft();
}

/**
 * Drop the size correction on its own, leaving the offset alone. Reachable from
 * the size control itself, so someone happy with their alignment does not have
 * to reset the pad as well.
 */
function resetCalibrationScale() {
    calibrationDraft.scale = 1;
    renderCalibrationDraft();
    setCalibrationStatus('Size correction cleared. Save to apply it.', 'info');
}

/**
 * Reset the whole working calibration: back to centred and to nominal size.
 * Does not touch what is stored until the dialog is saved.
 */
function resetCalibrationDraft() {
    calibrationDraft = neutralCalibration();
    renderCalibrationDraft();
    setCalibrationStatus('Offset and size correction reset. Save to apply it.', 'info');
}

/**
 * Fill the dialog's medium banner: name, product codes and whether this label
 * type already carries a stored calibration.
 */
function renderCalibrationMedium() {
    const nameEl = document.getElementById('cal-medium-name');
    const codesEl = document.getElementById('cal-medium-codes');
    const stateEl = document.getElementById('cal-medium-state');
    const entry = typeof labelEntryFor === 'function' ? labelEntryFor(calibrationLabelSize) : null;

    if (nameEl) {
        nameEl.textContent = calibrationMediumName(calibrationLabelSize);
    }
    if (codesEl) {
        const codes = entry && typeof labelCodesText === 'function' ? labelCodesText(entry) : '';
        codesEl.textContent = codes
            ? `${calibrationLabelSize} · ${codes}`
            : calibrationLabelSize;
    }
    if (stateEl) {
        stateEl.textContent = calibrationSaved
            ? `calibrated: ${describeCalibration(calibrationSaved)}`
            : 'not calibrated';
        stateEl.classList.toggle('is-set', !!calibrationSaved);
    }
}

/**
 * Request the calibration target for the open medium and show it. The target is
 * rendered WITHOUT the calibration on purpose — neither the offset nor the size
 * correction: it is the label as designed, which is exactly what the physical
 * print is compared against.
 */
function loadCalibrationTargetPreview() {
    const stage = document.getElementById('cal-stage');
    const image = document.getElementById('cal-preview');
    const empty = document.getElementById('cal-preview-empty');
    const hint = document.getElementById('cal-preview-hint');
    if (!stage || !image || !empty) return;

    /**
     * Fall back to the explanatory placeholder when no target can be shown.
     * @param {string} message - why there is no image
     */
    const showEmpty = (message) => {
        stage.classList.add('d-none');
        image.removeAttribute('src');
        empty.classList.remove('d-none');
        empty.textContent = message;
    };

    // Round die-cut media gets the same circular treatment as the main preview,
    // because that is where an off-centre print is most obvious.
    const diameter = typeof roundLabelDiameterMm === 'function'
        ? roundLabelDiameterMm(calibrationLabelSize)
        : null;
    stage.classList.toggle('is-round', diameter !== null);
    if (hint) {
        hint.textContent = diameter !== null
            ? `${diameter} mm round die-cut — only the circle ends up on the label`
            : 'The target as designed. Your calibration is applied when printing, never to this preview.';
    }

    showEmpty('Rendering the calibration target…');

    if (calibrationPreviewController) calibrationPreviewController.abort();
    const controller = new AbortController();
    calibrationPreviewController = controller;

    const body = buildCalibrationRequest(calibrationLabelSize, neutralCalibration());

    postCalibration('/api/v1/calibration/preview', body, controller.signal)
        .then(result => {
            if (calibrationPreviewController !== controller) return;
            if (result.ok && result.data.image) {
                image.src = result.data.image;
                stage.classList.remove('d-none');
                empty.classList.add('d-none');
                return;
            }
            showEmpty(result.missing
                ? 'This server build cannot render calibration targets yet. You can still enter a calibration and save it.'
                : `Target preview unavailable (${calibrationErrorMessage(result)})`);
        })
        .catch(error => {
            if (error && error.name === 'AbortError') return;
            console.error('Error rendering calibration target:', error);
            showEmpty('Target preview unavailable — the server could not be reached.');
        })
        .finally(() => {
            if (calibrationPreviewController === controller) {
                calibrationPreviewController = null;
            }
        });
}

/**
 * Open the calibration dialog for a label type.
 *
 * The label type comes from the settings form's <select id="label-size">, which
 * stays the single source of truth: opening the dialog for another medium
 * selects it there first, so the rest of the app follows along.
 *
 * @param {string} [identifier] - label type to calibrate; defaults to the
 *     currently selected one
 */
function openCalibrationDialog(identifier) {
    const labelSizeEl = document.getElementById('label-size');
    const modalEl = document.getElementById('calibrationModal');
    if (!labelSizeEl || !modalEl) return;

    if (identifier && identifier !== labelSizeEl.value) {
        labelSizeEl.value = identifier;
        labelSizeEl.dispatchEvent(new Event('change', { bubbles: true }));
    }

    calibrationLabelSize = labelSizeEl.value;
    if (!calibrationLabelSize) {
        showNotification('Select a label type first', 'warning');
        return;
    }

    calibrationSaved = storedCalibrationFor(calibrationLabelSize);
    calibrationDraft = calibrationSaved
        ? normaliseCalibration(calibrationSaved)
        : neutralCalibration();
    calibrationLastPrinted = null;
    calibrationStep = CALIBRATION_DEFAULT_STEP;
    calibrationScaleStep = CALIBRATION_DEFAULT_SCALE_STEP;

    renderCalibrationMedium();
    renderCalibrationStep();
    renderCalibrationDraft();
    setCalibrationStatus('');
    loadCalibrationTargetPreview();

    if (!calibrationModal && window.bootstrap && bootstrap.Modal) {
        calibrationModal = new bootstrap.Modal(modalEl);
    }
    if (calibrationModal) calibrationModal.show();
}

/**
 * Print a calibration target for the open medium with the working calibration —
 * it does not have to be saved first, so an offset and a size correction can
 * both be tried before they are committed. The job goes through the normal
 * print queue.
 * @param {boolean} dryRun - validate and report instead of printing
 */
async function printCalibrationTarget(dryRun) {
    const button = document.getElementById(dryRun ? 'cal-dry-run' : 'cal-test-print');
    if (!button) return;

    const originalHtml = button.innerHTML;
    button.disabled = true;
    button.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>' +
        (dryRun ? 'Checking...' : 'Printing...');

    try {
        const body = buildCalibrationRequest(calibrationLabelSize, calibrationDraft);
        if (dryRun) body.dry_run = true;

        const result = await postCalibration('/api/v1/calibration/test-print', body, null);

        if (!result.ok) {
            throw new Error(calibrationErrorMessage(result));
        }

        if (dryRun) {
            const would = result.data.would_print || {};
            const size = (would.width_px && would.height_px)
                ? ` (${would.width_px}x${would.height_px} px)`
                : '';
            const reachable = result.data.printer_reachable
                ? 'printer reachable'
                : 'printer NOT reachable';
            setCalibrationStatus(
                `Dry run OK — nothing was printed. Target for ${calibrationLabelSize}${size}, ${reachable}.`,
                result.data.printer_reachable ? 'success' : 'warning'
            );
        } else {
            calibrationLastPrinted = normaliseCalibration(calibrationDraft);
            setCalibrationStatus(
                `Test label queued with ${describeCalibration(calibrationLastPrinted)}. ` +
                'Measure how far it is still out, adjust by that much, and print again.',
                'success'
            );
            if (typeof refreshJobs === 'function') refreshJobs();
        }
        renderCalibrationDraft();
    } catch (error) {
        console.error('Error printing calibration target:', error);
        setCalibrationStatus(error.message, 'error');
    } finally {
        button.disabled = false;
        button.innerHTML = originalHtml;
    }
}

/**
 * Save the working calibration for the open medium, then keep the dialog open
 * so the loop can continue with the new value in effect.
 */
async function saveCalibrationDraft() {
    const button = document.getElementById('cal-save');
    if (!button) return;

    const originalHtml = button.innerHTML;
    button.disabled = true;
    button.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Saving...';

    try {
        const value = normaliseCalibration(calibrationDraft);
        await persistCalibration(calibrationLabelSize, value);

        calibrationSaved = calibrationIsNeutral(value) ? null : value;
        renderCalibrationMedium();
        renderCalibrationDraft();

        showNotification(
            calibrationIsNeutral(value)
                ? `Calibration removed for ${calibrationLabelSize}`
                : `Calibration saved for ${calibrationLabelSize}: ${describeCalibration(value)}`,
            'success'
        );
        setCalibrationStatus(
            calibrationIsNeutral(value)
                ? 'Saved. This label type prints exactly as previewed again.'
                : `Saved. Every print on ${calibrationLabelSize} will now ` +
                  `${describeCalibrationAction(value, false)}.`,
            'success'
        );
    } catch (error) {
        console.error('Error saving calibration:', error);
        showNotification(`Error saving calibration: ${error.message}`, 'error');
        setCalibrationStatus(`Could not save: ${error.message}`, 'error');
    } finally {
        button.innerHTML = originalHtml;
        renderCalibrationDraft();
    }
}

/**
 * Remove the stored calibration of a label type in a single click — offset and
 * size correction together — so a surprising result can always be undone
 * without hunting for the right numbers.
 * @param {string} identifier - label type to reset
 */
async function removeCalibration(identifier) {
    if (!identifier) return;

    try {
        await persistCalibration(identifier, null);
        showNotification(`Calibration removed for ${identifier} — it prints as previewed again`, 'success');

        // Keep an open dialog in sync when it shows the same medium.
        if (identifier === calibrationLabelSize) {
            calibrationSaved = null;
            calibrationDraft = neutralCalibration();
            renderCalibrationMedium();
            renderCalibrationDraft();
            setCalibrationStatus('Stored calibration removed.', 'success');
        }
    } catch (error) {
        console.error('Error removing calibration:', error);
        showNotification(`Error removing calibration: ${error.message}`, 'error');
    }
}

/**
 * Wire up the calibration UI: the Settings summary, the dialog's nudge pad,
 * size control, step buttons, number inputs and actions. Called from
 * setupEventListeners().
 */
function setupCalibration() {
    // ---- Settings summary ----
    const openButton = document.getElementById('calibration-open');
    if (openButton) {
        openButton.addEventListener('click', () => openCalibrationDialog());
    }

    const list = document.getElementById('calibration-list');
    if (list) {
        // Rows are re-rendered on every change, so both actions are delegated.
        list.addEventListener('click', event => {
            const edit = event.target.closest('[data-cal-edit]');
            if (edit) {
                openCalibrationDialog(edit.getAttribute('data-cal-edit'));
                return;
            }
            const clear = event.target.closest('[data-cal-clear]');
            if (clear) {
                removeCalibration(clear.getAttribute('data-cal-clear'));
            }
        });
    }

    // The summary names the selected medium, so it follows the label picker.
    const labelSizeEl = document.getElementById('label-size');
    if (labelSizeEl) {
        labelSizeEl.addEventListener('change', renderCalibrationSummary);
    }

    // ---- Dialog: nudge pad ----
    document.querySelectorAll('[data-cal-nudge]').forEach(button => {
        button.addEventListener('click', () => {
            nudgeCalibration(button.getAttribute('data-cal-nudge'));
        });
    });

    const padReset = document.getElementById('cal-pad-reset');
    if (padReset) {
        padReset.addEventListener('click', resetCalibrationDraft);
    }

    // ---- Dialog: size correction ----
    document.querySelectorAll('[data-cal-scale]').forEach(button => {
        button.addEventListener('click', () => {
            nudgeCalibrationScale(button.getAttribute('data-cal-scale'));
        });
    });

    const scaleReset = document.getElementById('cal-scale-reset');
    if (scaleReset) {
        scaleReset.addEventListener('click', resetCalibrationScale);
    }

    // ---- Dialog: step size (millimetres and percent) ----
    document.querySelectorAll('[data-cal-step]').forEach(button => {
        button.addEventListener('click', () => {
            const step = parseFloat(button.getAttribute('data-cal-step'));
            if (Number.isFinite(step)) {
                calibrationStep = step;
                renderCalibrationStep();
            }
        });
    });
    document.querySelectorAll('[data-cal-scale-step]').forEach(button => {
        button.addEventListener('click', () => {
            const step = parseFloat(button.getAttribute('data-cal-scale-step'));
            if (Number.isFinite(step)) {
                calibrationScaleStep = step;
                renderCalibrationStep();
            }
        });
    });

    // ---- Dialog: raw numbers ----
    ['cal-x', 'cal-y'].forEach(id => {
        const input = document.getElementById(id);
        if (!input) return;
        const axis = id === 'cal-x' ? 'x_mm' : 'y_mm';
        input.addEventListener('input', () => {
            calibrationDraft[axis] = clampMm(input.value);
            renderCalibrationDraft();
        });
        // Normalise the field itself once the user is done with it.
        input.addEventListener('change', () => {
            calibrationDraft[axis] = clampMm(input.value);
            input.value = formatMm(calibrationDraft[axis]);
            renderCalibrationDraft();
        });
    });

    // The size field is entered in percent away from nominal, which is what
    // "print 2 % smaller" means; the stored multiplier is derived from it.
    const scaleInput = document.getElementById('cal-scale');
    if (scaleInput) {
        scaleInput.addEventListener('input', () => {
            calibrationDraft.scale = scaleFromPercent(scaleInput.value);
            renderCalibrationDraft();
        });
        scaleInput.addEventListener('change', () => {
            calibrationDraft.scale = scaleFromPercent(scaleInput.value);
            scaleInput.value = formatPercent(percentFromScale(calibrationDraft.scale));
            renderCalibrationDraft();
        });
    }

    // ---- Dialog: actions ----
    const testPrint = document.getElementById('cal-test-print');
    if (testPrint) {
        testPrint.addEventListener('click', () => printCalibrationTarget(false));
    }
    const dryRun = document.getElementById('cal-dry-run');
    if (dryRun) {
        dryRun.addEventListener('click', () => printCalibrationTarget(true));
    }
    const save = document.getElementById('cal-save');
    if (save) {
        save.addEventListener('click', saveCalibrationDraft);
    }
    const remove = document.getElementById('cal-remove');
    if (remove) {
        remove.addEventListener('click', () => removeCalibration(calibrationLabelSize));
    }

    // Drop an in-flight target render when the dialog is dismissed.
    const modalEl = document.getElementById('calibrationModal');
    if (modalEl) {
        modalEl.addEventListener('hidden.bs.modal', () => {
            if (calibrationPreviewController) {
                calibrationPreviewController.abort();
                calibrationPreviewController = null;
            }
        });
    }

    renderCalibrationStep();
    renderCalibrationSummary();
}
