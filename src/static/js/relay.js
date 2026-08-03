// Brother QL Printer App - Relay power control
//
// A Brother QL powers itself up as soon as mains power returns, so a printer
// whose supply runs through a relay can be left switched off at the wall and
// still print: the job arrives, the relay closes, the printer boots and prints
// it. Two events carry that. "turn_on" fires when a job arrives at a printer
// that is not answering, and "turn_off" fires once everything has wound down.
//
// The part a user has to understand is the timing, because three timers are
// involved and only two of them belong to this app. With relay power control
// on, the printer's own interval is taken off the keep-alive window so the
// device sleeps at the moment that was asked for:
//
//     0:00  last print
//     3:50  keep-alive heartbeat stops   (the configured window minus the
//                                         printer's own auto-power-off interval)
//     4:00  the printer powers itself off (exactly the window configured)
//     4:05  turn_off is sent              (after the safety margin, to a device
//                                          that is already off)
//
// With it off, nothing is taken off anything: the heartbeat runs the whole
// window and the device's own timer starts after it, so the printer sleeps at
// 4:10 instead. Both shapes are correct, and the status payload says which one
// is in force (hardware_offset_applied) so this file never has to guess.
//
// None of those numbers are worked out here. GET /printers/relay-power returns
// the whole chain already derived, and this module only formats it: one place
// decides what the timing is, and it is the same place that acts on it. The one
// request this module makes that changes anything is
// POST /printers/relay-power/send, which fires a single webhook on request.
//
// The status endpoint also carries the warning about the printer's built-in
// auto-power-off, which the app can neither read nor change. That wording is
// shown verbatim rather than paraphrased, for the same reason: one source.
//
// All of it lives inline in Settings, under Keep Alive, and unfolds from the
// master switch: with relay power control off there is an introduction and that
// switch, and nothing else. The fields are part of the Settings form and are
// written by its own Save, so there is one save button on that page and one set
// of rules checked before it is pressed.

// The auto-power-off intervals the printer's own menu offers, in minutes. This
// is not a range with a step — the device has exactly these six entries and no
// free field, so a number outside them could not describe any real printer.
const RELAY_AUTO_POWER_OFF_CHOICES = [10, 20, 30, 40, 50, 60];

// Where the last warning the server sent is remembered between visits.
//
// The status endpoint only carries the warning while relay power control and
// its turn_off half are both switched on, and the moment it matters most is the
// one before that — while the user is deciding. Remembering the server's own
// words lets them be shown then, without this file keeping a second copy of the
// text that could drift from the first.
const RELAY_WARNING_STORAGE_KEY = 'relay-power-warning';

// How often the countdown to the scheduled turn-off is repainted. This is a
// local repaint of a number the server already gave us, NOT a poll: the relay
// status is refreshed by the printer-status poll that already runs (see
// refreshRelayStatus's callers), and everything in between is arithmetic on the
// absolute timestamp that poll returned.
const RELAY_TICK_MS = 1000;

// The relay settings exactly as last read from GET /settings. The configuration
// is read from there rather than from the status endpoint so that the block
// still describes itself correctly on a server that has no status endpoint yet.
let relaySettings = {};

// The last status payload, or null when the endpoint could not be read.
let relayStatus = null;

// Whether the server answered the status endpoint at all. False means "this
// build has no relay status", which is a different thing from "the feature is
// off" and is said differently.
let relayStatusAvailable = false;

// Difference between the server's clock and ours, in milliseconds, measured
// from the two views of the same moment that the status payload carries. The
// countdown is driven from the absolute timestamp corrected by this, so it
// neither drifts nor jumps when the two machines disagree.
let relayClockSkewMs = 0;

// The warning text as the server words it, or '' when it has never been seen.
let relayWarningText = '';

// Handle of the countdown repaint timer; null while nothing is counting down.
let relayTicker = null;

// Where a webhook fired by hand is sent. A POST with the action in the body,
// so neither half can be reached by following a link, and one endpoint answers
// for both.
const RELAY_FIRE_ENDPOINT = '/api/v1/printers/relay-power/send';

// Whether this server build has the endpoint at all. It is assumed to be there
// until it answers otherwise once, at which point the buttons are withdrawn:
// the feature itself works fine without it, so a build that cannot test by hand
// is a missing convenience and not a fault to shout about.
let relayFireSupported = true;

// True while a hand-fired webhook is in flight, so the buttons cannot stack.
let relayFireInFlight = false;

// Whether the printer answered the last status check: true, false, or null when
// nothing has been read yet. It is written by that check (see
// relayNotePrinterReachable) rather than asked for here — cutting mains power
// must not depend on a request of its own that could be the one that fails.
let relayPrinterReachable = null;

/**
 * Loose truthiness for a flag that may arrive as a boolean, a number or a
 * string ("true", "1", "yes").
 * @param {*} value
 * @returns {boolean}
 */
function relayTruthy(value) {
    if (typeof value === 'boolean') return value;
    if (typeof value === 'number') return value !== 0;
    if (typeof value === 'string') {
        const text = value.trim().toLowerCase();
        return text === 'true' || text === '1' || text === 'yes' || text === 'on';
    }
    return false;
}

/**
 * Read a number out of a payload, returning null for anything unusable. Used on
 * every server-supplied duration, all of which are explicitly nullable.
 * @param {*} value
 * @returns {?number}
 */
function relayNumber(value) {
    if (typeof value === 'number' && Number.isFinite(value)) return value;
    if (typeof value === 'string' && value.trim() !== '') {
        const parsed = Number(value);
        if (Number.isFinite(parsed)) return parsed;
    }
    return null;
}

/**
 * Format an offset from the last print as h:mm, the way the timing chain is
 * written down — seconds are only shown when there are any, which there are not
 * for any value a user can configure.
 * @param {number} seconds - offset in seconds (negative is treated as zero)
 * @returns {string}
 */
function formatRelayClock(seconds) {
    const total = Math.max(0, Math.round(seconds));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const rest = total % 60;
    const base = hours + ':' + String(minutes).padStart(2, '0');
    return rest === 0 ? base : base + ':' + String(rest).padStart(2, '0');
}

/**
 * Format a remaining time for the countdown: h:mm:ss, or mm:ss under an hour.
 * @param {number} seconds
 * @returns {string}
 */
function formatRelayCountdown(seconds) {
    const total = Math.max(0, Math.round(seconds));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const rest = total % 60;
    if (hours > 0) {
        return hours + ':' + String(minutes).padStart(2, '0') + ':' + String(rest).padStart(2, '0');
    }
    return String(minutes).padStart(2, '0') + ':' + String(rest).padStart(2, '0');
}

/**
 * Format a duration in words, for the prose that names an interval rather than
 * a moment ("3 h 50 min").
 * @param {number} seconds
 * @returns {string}
 */
function formatRelayDuration(seconds) {
    const total = Math.max(0, Math.round(seconds));
    if (total === 0) return 'nothing';
    const hours = Math.floor(total / 3600);
    const minutes = Math.round((total % 3600) / 60);
    if (hours === 0) return minutes + ' min';
    if (minutes === 0) return hours + ' h';
    return hours + ' h ' + minutes + ' min';
}

/**
 * Format a server timestamp (ISO-8601 UTC) as a local time, or '' when it
 * cannot be read.
 * @param {*} value
 * @returns {string}
 */
function formatRelayMoment(value) {
    if (!value) return '';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '';
    return date.toLocaleString(undefined, {
        month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'
    });
}

/**
 * Remember the warning the server sent, so it can be shown while the user is
 * still deciding whether to switch the turn_off half on — which is before the
 * server will send it again. Storage failures are ignored: a private-mode
 * browser simply falls back to showing it only while it is being sent.
 * @param {string} text
 */
function rememberRelayWarning(text) {
    relayWarningText = text;
    try {
        window.localStorage.setItem(RELAY_WARNING_STORAGE_KEY, text);
    } catch (error) {
        // No storage available; the in-memory copy still serves this session.
    }
}

/**
 * Load the remembered warning at start-up.
 */
function loadRelayWarning() {
    if (relayWarningText) return;
    try {
        relayWarningText = window.localStorage.getItem(RELAY_WARNING_STORAGE_KEY) || '';
    } catch (error) {
        relayWarningText = '';
    }
}

// ===================== Reading the configuration =====================

/**
 * Take the relay settings out of a settings document. Called from
 * loadSettings(), and again after every save of the Settings form.
 *
 * Every key is optional: a settings document from a server that predates the
 * feature simply describes a relay that is switched off, which is what such a
 * server does.
 *
 * @param {*} settings - the parsed body of GET /settings
 */
function applyRelaySettings(settings) {
    const source = (settings && typeof settings === 'object') ? settings : {};

    const minutes = relayNumber(source.printer_auto_power_off_minutes);
    const delay = relayNumber(source.relay_webhook_turn_off_delay_minutes);

    relaySettings = {
        enabled: relayTruthy(source.relay_webhook_enabled),
        turn_on_url: typeof source.relay_webhook_turn_on_url === 'string'
            ? source.relay_webhook_turn_on_url : '',
        turn_off_url: typeof source.relay_webhook_turn_off_url === 'string'
            ? source.relay_webhook_turn_off_url : '',
        turn_off_enabled: relayTruthy(source.relay_webhook_turn_off_enabled),
        turn_off_delay_minutes: delay === null ? 5 : delay,
        auto_power_off_minutes: RELAY_AUTO_POWER_OFF_CHOICES.indexOf(minutes) !== -1 ? minutes : 10,
        // The keep-alive half of the timing chain. It is not a relay setting,
        // but every constraint the server enforces is about the relationship
        // between the two, so the stored values are kept alongside.
        keep_alive_enabled: relayTruthy(source.keep_alive_enabled),
        keep_alive_mode: typeof source.keep_alive_mode === 'string' ? source.keep_alive_mode : 'forever',
        keep_alive_duration_seconds: relayNumber(source.keep_alive_duration_seconds) || 0
    };

    // The fields are part of the Settings form, so they are filled from the
    // stored document exactly like every other field on that page is.
    fillRelayFields();
    renderRelayUI();
}

/**
 * Take in what the Settings form has just written, so the stored copy this
 * module checks against is the one now in the file.
 *
 * Both halves matter. The relay keys are the ones this block put in the body,
 * and the keep-alive window is the other side of both rules the server
 * enforces: the printer's own interval is subtracted from it, and the turn-off
 * moment is measured from its end.
 *
 * @param {Object} saved - the body that was PUT to /settings
 */
function relayNoteSettingsSaved(saved) {
    const body = (saved && typeof saved === 'object') ? saved : {};

    // Whether the half that can cut mains power has just been switched on. The
    // server only issues its warning once that is stored, so this is the moment
    // to make sure it has been read.
    const arming = relayTruthy(body.relay_webhook_turn_off_enabled) && !relaySettings.turn_off_enabled;

    applyRelaySettings(Object.assign({}, relayStoredDocument(), body));
    // The chain the server derives has moved with it.
    refreshRelayStatus();

    if (arming && relayWarningText && typeof showNotification === 'function') {
        // Not a second copy of the wording: the same string, said once more at
        // the moment the thing it warns about becomes possible.
        showNotification(relayWarningText, 'warning', 15000);
    }
}

/**
 * The stored settings, back in the shape of a settings document, so a patch can
 * be merged onto them without re-reading the server.
 * @returns {Object}
 */
function relayStoredDocument() {
    return {
        relay_webhook_enabled: relaySettings.enabled,
        relay_webhook_turn_on_url: relaySettings.turn_on_url,
        relay_webhook_turn_off_url: relaySettings.turn_off_url,
        relay_webhook_turn_off_enabled: relaySettings.turn_off_enabled,
        relay_webhook_turn_off_delay_minutes: relaySettings.turn_off_delay_minutes,
        printer_auto_power_off_minutes: relaySettings.auto_power_off_minutes,
        keep_alive_enabled: relaySettings.keep_alive_enabled,
        keep_alive_mode: relaySettings.keep_alive_mode,
        keep_alive_duration_seconds: relaySettings.keep_alive_duration_seconds
    };
}

/**
 * The relay half of the settings document, read from the fields, for the
 * Settings form's own save to carry. The same shape the Media block uses.
 * @returns {Object}
 */
function relaySettingsPatch() {
    const values = relayFormValues();
    return {
        relay_webhook_enabled: values.enabled,
        relay_webhook_turn_on_url: values.turn_on_url,
        relay_webhook_turn_off_url: values.turn_off_url,
        relay_webhook_turn_off_enabled: values.turn_off_enabled,
        relay_webhook_turn_off_delay_minutes: values.turn_off_delay_minutes,
        printer_auto_power_off_minutes: values.auto_power_off_minutes
    };
}

// ===================== Reading the live status =====================

/**
 * Refresh the relay status.
 *
 * Deliberately has no timer of its own. It is called from the printer-status
 * check, which already runs on load, every 30 s and on the manual refresh
 * button — so the relay follows the same cadence as everything else in the top
 * bar, at the cost of one GET per cycle. The countdown between those cycles is
 * repainted locally from the absolute timestamp this returns.
 *
 * @returns {Promise<boolean>} whether a status was read
 */
async function refreshRelayStatus() {
    try {
        const response = await fetch('/api/v1/printers/relay-power');
        if (!response.ok) {
            // 404/405/501 all mean "this build has no relay status endpoint",
            // which is not the same as "the relay is off" and is said so.
            relayStatusAvailable = false;
            relayStatus = null;
            renderRelayUI();
            return false;
        }

        const data = await response.json();
        if (!data || typeof data !== 'object') {
            relayStatusAvailable = false;
            relayStatus = null;
            renderRelayUI();
            return false;
        }

        relayStatus = data;
        // An empty object is what a mocked or stubbed backend answers; it says
        // nothing, so it is treated as "no status", not as "off".
        relayStatusAvailable = Object.keys(data).length > 0;

        // Two readings of the same moment, so the countdown can be driven from
        // the absolute timestamp without inheriting a clock difference.
        const scheduled = relayNumber(data.scheduled_turn_off_at);
        const remaining = relayNumber(data.seconds_until_turn_off);
        relayClockSkewMs = (scheduled !== null && remaining !== null)
            ? scheduled * 1000 - (Date.now() + remaining * 1000)
            : 0;

        if (typeof data.warning === 'string' && data.warning.trim()) {
            rememberRelayWarning(data.warning);
        }

        renderRelayUI();
        return true;
    } catch (error) {
        console.error('Error reading the relay power status:', error);
        relayStatusAvailable = false;
        relayStatus = null;
        renderRelayUI();
        return false;
    }
}

/**
 * Milliseconds left until the scheduled turn-off, or null when nothing is
 * scheduled. Corrected for the difference between the two clocks.
 * @returns {?number}
 */
function relayRemainingMs() {
    if (!relayStatus) return null;
    const scheduled = relayNumber(relayStatus.scheduled_turn_off_at);
    if (scheduled === null) return null;
    return scheduled * 1000 - relayClockSkewMs - Date.now();
}

// ===================== The countdown =====================

/**
 * Start repainting the countdown once a second, if there is one to repaint.
 *
 * The timer exists only while a countdown is on screen and the tab is visible,
 * and it makes no requests: it reads the clock. When it runs out it asks the
 * server once, because the state it was counting towards has just changed.
 */
function startRelayCountdown() {
    if (relayTicker !== null) return;
    if (typeof document !== 'undefined' && document.hidden) return;
    if (relayRemainingMs() === null) return;

    relayTicker = setInterval(() => {
        const remaining = relayRemainingMs();
        if (remaining === null) {
            stopRelayCountdown();
            return;
        }
        if (remaining <= 0) {
            stopRelayCountdown();
            // The moment has passed: what the server reports now is a fact
            // rather than a prediction, so read it once instead of counting
            // into negative numbers.
            refreshRelayStatus();
            return;
        }
        paintRelayCountdown();
    }, RELAY_TICK_MS);
}

/**
 * Stop the countdown repaint timer.
 */
function stopRelayCountdown() {
    if (relayTicker !== null) {
        clearInterval(relayTicker);
        relayTicker = null;
    }
}

/**
 * Repaint just the countdown number. Nothing else in the block changes between
 * server reads, so nothing else is touched.
 */
function paintRelayCountdown() {
    const remaining = relayRemainingMs();
    const el = document.getElementById('relay-countdown');
    if (el) el.textContent = remaining === null ? '' : formatRelayCountdown(remaining / 1000);
}

// ===================== Rendering =====================

/**
 * One line saying what state the feature is in, as it is STORED — which is what
 * the app is actually doing, whatever the fields currently say.
 * @returns {{flag: string, detail: string, tone: string}}
 */
function relayStateLine() {
    if (!relaySettings.enabled) {
        return {
            flag: 'off',
            tone: 'idle',
            detail: 'The printer is never switched by this app.'
        };
    }
    if (!relaySettings.turn_off_enabled) {
        return {
            flag: 'on',
            tone: 'on',
            detail: 'Switches the printer on for a job; never switches it off.'
        };
    }
    return {
        flag: 'on, turn-off armed',
        tone: 'armed',
        detail: 'Switches the printer on for a job, and cuts mains power when the window closes.'
    };
}

/**
 * Paint the state block: what the feature is doing right now, the countdown to
 * the next turn-off, the outcome of the last webhook and the last failure. It
 * is the ONE statement of the current state on this page — the fields under it
 * say what is configured, and this says what is running.
 *
 * It is hidden outright while the relay is off both in the fields and in the
 * file, which is the state a reader who does not use this feature is in. It
 * stays up while the two disagree, so that switching the master switch off does
 * not take the notice that the relay is still armed away with it.
 */
function renderRelaySummary() {
    const box = document.getElementById('relay-summary');
    if (!box) return;

    const values = relayFormValues();
    if (!relaySettings.enabled && !values.enabled) {
        box.innerHTML = '';
        box.hidden = true;
        return;
    }
    box.hidden = false;

    const state = relayStateLine();
    const rows = [];

    rows.push(
        '<div class="relay-summary-head">' +
            '<span class="relay-summary-state relay-state--' + state.tone + '">' + escapeHtml(state.flag) + '</span>' +
            '<span class="relay-summary-detail">' + escapeHtml(state.detail) + '</span>' +
        '</div>'
    );

    if (relaySettings.enabled) {
        const remaining = relayRemainingMs();
        if (remaining !== null && remaining > 0) {
            rows.push(
                '<div class="relay-summary-row">' +
                    '<i class="bi bi-hourglass-split" aria-hidden="true"></i>' +
                    '<span>Mains power switches off in ' +
                        '<span class="relay-countdown mono" id="relay-countdown">' +
                        escapeHtml(formatRelayCountdown(remaining / 1000)) + '</span>' +
                    '</span>' +
                '</div>'
            );
        } else if (relaySettings.turn_off_enabled && relayStatusAvailable) {
            rows.push(
                '<div class="relay-summary-row">' +
                    '<i class="bi bi-hourglass" aria-hidden="true"></i>' +
                    '<span>Nothing is scheduled. The window starts at the next print.</span>' +
                '</div>'
            );
        }

        const action = relayStatus && relayStatus.last_action;
        if (action) {
            const when = formatRelayMoment(relayStatus.last_action_at);
            rows.push(
                '<div class="relay-summary-row">' +
                    '<i class="bi bi-check-circle-fill relay-icon-ok" aria-hidden="true"></i>' +
                    '<span>Last webhook: <span class="mono">' + escapeHtml(String(action)) + '</span>' +
                    (when ? ' at ' + escapeHtml(when) : '') + '</span>' +
                '</div>'
            );
        }

        const error = relayStatus && relayStatus.last_error;
        if (error) {
            const when = formatRelayMoment(relayStatus.last_error_at);
            rows.push(
                '<div class="relay-summary-row relay-summary-row--error">' +
                    '<i class="bi bi-exclamation-circle-fill relay-icon-error" aria-hidden="true"></i>' +
                    '<span>Last failure' + (when ? ' at ' + escapeHtml(when) : '') + ': ' +
                        escapeHtml(String(error)) + '</span>' +
                '</div>'
            );
        }

        if (relayStatus && relayStatus.authorization_configured) {
            rows.push(
                '<div class="relay-summary-row">' +
                    '<i class="bi bi-key-fill" aria-hidden="true"></i>' +
                    '<span>An authorization header is sent with each webhook, from the ' +
                        'environment.</span>' +
                '</div>'
            );
        }

        if (!relayStatusAvailable) {
            rows.push(
                '<div class="relay-summary-row">' +
                    '<i class="bi bi-info-circle-fill" aria-hidden="true"></i>' +
                    '<span>This server build does not report relay status yet, so there is no ' +
                        'timing chain and no countdown to show.</span>' +
                '</div>'
            );
        }
    }

    // The fields below are part of the Settings form, so an edit in them is not
    // in force until that form is saved. Everything else in this block, and the
    // two test buttons, describe what IS in force — which is worth saying once,
    // here, rather than repeating next to each field it applies to.
    if (relayFieldsAreUnsaved()) {
        rows.push(
            '<div class="relay-summary-row">' +
                '<i class="bi bi-pencil-fill" aria-hidden="true"></i>' +
                '<span>These fields have unsaved changes. Save Settings to apply them.</span>' +
            '</div>'
        );
    }

    box.innerHTML = rows.join('');
}

/**
 * Whether the relay fields say something other than what is stored.
 * @returns {boolean}
 */
function relayFieldsAreUnsaved() {
    const values = relayFormValues();
    return values.enabled !== !!relaySettings.enabled ||
        values.turn_on_url !== String(relaySettings.turn_on_url || '') ||
        values.turn_off_url !== String(relaySettings.turn_off_url || '') ||
        values.turn_off_enabled !== !!relaySettings.turn_off_enabled ||
        values.turn_off_delay_minutes !== relaySettings.turn_off_delay_minutes ||
        values.auto_power_off_minutes !== relaySettings.auto_power_off_minutes;
}

/**
 * Whether the printer's own auto-power-off interval was actually taken off the
 * keep-alive window to produce the heartbeat's length.
 *
 * It only is while relay power control is on. With the feature off the window
 * is left exactly as configured, deliberately, so that an installation ignoring
 * the relay sees no change in its timing at all.
 *
 * The server reports it (hardware_offset_applied), and that flag is the
 * authority. A build too old to send it is read off its numbers instead: the
 * interval that would be taken off is one of six values and the smallest is ten
 * minutes, so a heartbeat exactly as long as the window is one that had nothing
 * taken off it. That reading cannot produce a false negative, and it is the
 * case the wording most needs to get right.
 *
 * @returns {?boolean} null when neither the flag nor the numbers can say
 */
function relaySubtractionApplied() {
    if (!relayStatus) return null;
    if (typeof relayStatus.hardware_offset_applied === 'boolean') {
        return relayStatus.hardware_offset_applied;
    }

    const window_ = relayNumber(relayStatus.configured_window_seconds);
    const effective = relayNumber(relayStatus.effective_keep_alive_seconds);
    const hardware = relayNumber(relayStatus.printer_auto_power_off_minutes);
    if (window_ === null || effective === null || hardware === null || hardware <= 0) return null;
    return effective !== window_;
}

/**
 * Paint the timing chain: the four moments, counted from the last print.
 *
 * Every number in it comes from the status endpoint, which derives the whole
 * chain from the same settings the service acts on. Nothing is recomputed here.
 * The single exception is the turn_off row, whose moment is the sum of a window
 * and a margin that the endpoint reports separately.
 */
function renderRelayChain() {
    const list = document.getElementById('relay-chain');
    const note = document.getElementById('relay-chain-note');
    if (!list) return;

    if (!relayStatusAvailable || !relayStatus) {
        list.innerHTML = '<li class="relay-chain-empty">The timing chain is worked out by the ' +
            'server, and this build does not report it yet.</li>';
        if (note) note.textContent = '';
        return;
    }

    const window_ = relayNumber(relayStatus.configured_window_seconds);
    const effective = relayNumber(relayStatus.effective_keep_alive_seconds);
    const delay = relayNumber(relayStatus.turn_off_delay_seconds);
    const hardware = relayNumber(relayStatus.printer_auto_power_off_minutes);

    if (window_ === null) {
        list.innerHTML = '<li class="relay-chain-empty">Keep Alive is not running for a set time ' +
            'after each print, so there is no window to measure from and nothing is scheduled.</li>';
        if (note) note.textContent = '';
        return;
    }

    /**
     * One row of the chain.
     * @param {number} offset - seconds after the last print
     * @param {string} what - the event
     * @param {string} why - the reason, in the numbers it comes from
     * @param {string} [tone] - extra class for the row
     */
    const row = (offset, what, why, tone) => (
        '<li class="relay-chain-row' + (tone ? ' ' + tone : '') + '">' +
            '<span class="relay-chain-time mono">' + escapeHtml(formatRelayClock(offset)) + '</span>' +
            '<span class="relay-chain-body">' +
                '<span class="relay-chain-what">' + escapeHtml(what) + '</span>' +
                (why ? '<span class="relay-chain-why">' + escapeHtml(why) + '</span>' : '') +
            '</span>' +
        '</li>'
    );

    // Whether the printer's own interval is coming off the window. It is the
    // difference between the two shapes this chain can have, so both the
    // heartbeat row and the row after it are written from it.
    const subtracted = relaySubtractionApplied();
    const hardwareText = hardware === null ? 'own interval' : 'own ' + hardware + ' min';

    // When the device's own timer expires, which is not the same as the end of
    // the window unless the subtraction happened. The server works it out, for
    // the same reason it works out everything else here; on a build that does
    // not report it the two numbers it does report are added, which is the same
    // arithmetic and both terms are still its own.
    const serverPowerOff = relayNumber(relayStatus.printer_power_off_seconds);
    const powerOff = serverPowerOff !== null
        ? serverPowerOff
        : ((effective !== null && hardware !== null) ? effective + hardware * 60 : null);

    const rows = [row(0, 'Last print', 'The window starts here, and any new job starts it again.')];

    if (effective === null) {
        rows.push(row(0, 'The heartbeat runs continuously', 'Keep Alive is not timed.'));
    } else if (effective === 0) {
        rows.push(row(0, 'The keep-alive heartbeat does nothing',
            'The window is exactly the printer\'s own interval, so the hardware carries all of it.',
            'is-quiet'));
    } else if (subtracted === false) {
        // Nothing was taken off, so saying it was would describe a different
        // installation. Name the reason instead: the subtraction belongs to the
        // relay, and the relay is not switched on.
        rows.push(row(effective, 'The keep-alive heartbeat stops',
            'The whole ' + formatRelayDuration(window_) + ' window, with nothing taken off it. ' +
            'The printer\'s ' + hardwareText + ' is subtracted only while relay power control is ' +
            'on, so leaving the relay off leaves this timing exactly as it has always been.'));
    } else {
        rows.push(row(effective, 'The keep-alive heartbeat stops',
            'The ' + formatRelayDuration(window_) + ' window minus the printer\'s ' + hardwareText + '.'));
    }

    if (subtracted === false && powerOff !== null) {
        // Without the subtraction the device's timer starts after the window
        // rather than inside it, so the printer sleeps that much later than the
        // window closes. Two rows reading the same time is what made the chain
        // contradict itself.
        rows.push(row(powerOff, 'The printer powers itself off',
            'Its ' + hardwareText + ' timer starts when the heartbeat stops, so the printer sleeps ' +
            'that much after the window rather than at the end of it.'));
    } else {
        rows.push(row(powerOff === null ? window_ : powerOff, 'The printer powers itself off',
            'Its own timer, started when the heartbeat stopped. That lands on exactly the window ' +
            'configured.'));
    }

    if (relaySettings.turn_off_enabled && delay !== null) {
        rows.push(row(window_ + delay, 'turn_off is sent',
            'After a ' + formatRelayDuration(delay) + ' safety margin, to a printer that should ' +
            'already be off.', 'is-cut'));
    } else {
        // Why it is not sent, which is a setting and not a fact of the timing.
        // The two reasons are different switches and are worth telling apart.
        const why = !relaySettings.enabled
            ? 'Relay power control is off in these settings, so the app never switches the mains. ' +
              'Mains power stays on and the printer sleeps on its own.'
            : 'Send turn_off is set to "Never" in these settings. Mains power stays on and the ' +
              'printer sleeps on its own.';
        rows.push(
            '<li class="relay-chain-row is-quiet">' +
                '<span class="relay-chain-time mono">—</span>' +
                '<span class="relay-chain-body">' +
                    '<span class="relay-chain-what">turn_off is not sent</span>' +
                    '<span class="relay-chain-why">' + escapeHtml(why) + '</span>' +
                '</span>' +
            '</li>'
        );
    }

    list.innerHTML = rows.join('');

    if (note) {
        note.textContent = 'Counted from the last print, as h:mm. These are the stored settings ' +
            'as the server reads them, so save an edit to see it here.';
    }
}

/**
 * Show the warning about the printer's built-in auto-power-off, in the server's
 * own words, directly under the setting it is about.
 *
 * The text is never written here. It arrives with the status while the turn_off
 * half is switched on, and is remembered so it can also be shown in the moment
 * before that — which is the moment it is for.
 */
function renderRelayWarning() {
    const box = document.getElementById('relay-warning');
    const text = document.getElementById('relay-warning-text');
    if (!box || !text) return;

    if (relayWarningText) {
        text.textContent = relayWarningText;
        box.hidden = false;
        box.classList.remove('is-pending');
        return;
    }

    // Nothing has been read yet. Say where it comes from rather than inventing
    // a version of it: two wordings of a safety warning is one too many.
    text.textContent = 'The safety warning for this setting is issued by the server and appears ' +
        'here as soon as it can be read.';
    box.hidden = false;
    box.classList.add('is-pending');
}

/**
 * Paint everything the relay owns that is driven by state rather than by the
 * fields: the state block, the chain, the warning, the constraint line, the
 * test buttons and the countdown timer.
 */
function renderRelayUI() {
    renderRelaySummary();
    renderRelayChain();
    renderRelayWarning();
    renderRelayConstraint();
    renderRelayFireButtons();

    if (relayRemainingMs() !== null) {
        startRelayCountdown();
    } else {
        stopRelayCountdown();
    }
}

// ===================== The constraints the server enforces =====================

/**
 * Read the keep-alive controls in the Settings form, which is what the user is
 * looking at and what the next save of that form will send.
 * @returns {{enabled: boolean, mode: string, duration: number}}
 */
function relayFormKeepAlive() {
    const enabledEl = document.getElementById('keep-alive-enabled');
    const modeEl = document.getElementById('keep-alive-mode');
    const valueEl = document.getElementById('keep-alive-duration-value');
    const unitEl = document.getElementById('keep-alive-duration-unit');

    const value = parseInt(valueEl && valueEl.value, 10) || 0;
    const unit = unitEl ? unitEl.value : 'hours';

    return {
        enabled: !!enabledEl && enabledEl.value === 'true',
        mode: modeEl ? modeEl.value : 'forever',
        duration: unit === 'hours' ? value * 3600 : value * 60
    };
}

/**
 * Check a proposed combination against the two rules the server enforces, and
 * return the reason it would be refused.
 *
 * Both rules are about the relationship between the relay and the keep-alive
 * window, so both need the two halves together:
 *
 *   * turn_off is measured from the end of a timed keep-alive window, so there
 *     has to be one;
 *   * the printer's own interval is subtracted from that window, so the window
 *     cannot be shorter than the interval. Equal is allowed and is a real
 *     configuration.
 *
 * @param {Object} relay - {enabled, turn_on_url, turn_off_enabled, auto_power_off_minutes}
 * @param {{enabled: boolean, mode: string, duration: number}} keepAlive
 * @returns {string} the reason, or '' when the combination is acceptable
 */
function relayConstraintMessage(relay, keepAlive) {
    if (!relay || !relay.enabled) return '';

    if (!String(relay.turn_on_url || '').trim()) {
        return 'A turn-on webhook URL is required while relay power control is on. Without ' +
            'one there is nothing to call to switch the printer on.';
    }

    const hardwareSeconds = (relay.auto_power_off_minutes || 10) * 60;
    const hasWindow = keepAlive.enabled && keepAlive.mode === 'timed' && keepAlive.duration > 0;

    if (hasWindow && keepAlive.duration < hardwareSeconds) {
        return 'The keep-alive window (' + formatRelayDuration(keepAlive.duration) + ') is shorter ' +
            'than the printer\'s own auto-power-off interval (' + (relay.auto_power_off_minutes || 10) +
            ' min). The interval is subtracted from the window, so the window cannot be shorter ' +
            'than it. Raise the keep-alive duration to at least ' + (relay.auto_power_off_minutes || 10) +
            ' min, or set a shorter interval on the printer and here.';
    }

    if (relay.turn_off_enabled && !hasWindow) {
        return 'Sending turn_off needs Keep Alive enabled in "For a set time after each print" mode ' +
            'with a duration above zero. The turn-off moment is measured from the end of that ' +
            'window; without one there is no moment at which cutting mains power would be known ' +
            'to be safe.';
    }

    // Not a server rule, and only the fields can be in this state: "its own
    // URL" is chosen with the box still empty, which would store the shared
    // answer without saying so.
    if (relay.turn_off_url_mode === 'own' && !String(relay.turn_off_url || '').trim()) {
        return 'Turn off is set to use its own URL, and there is none yet. Enter it, or switch ' +
            'back to using the same URL as turn on.';
    }

    return '';
}

/**
 * Paint the constraint line: what would stop the next save of the Settings
 * form, before it is attempted.
 *
 * It reads the fields rather than the file, because the fields are what that
 * save will send — and both rules are enforced on the settings document as a
 * whole: an enabled relay with no turn-on URL, a window shorter than the
 * printer's own interval, or turn_off armed without a timed window is refused.
 * Saying so here keeps the refusal from arriving as a failed save of everything
 * else in the form as well.
 */
function renderRelayConstraint() {
    const box = document.getElementById('relay-constraint');
    if (!box) return;

    const message = relayConstraintMessage(relayFormValues(), relayFormKeepAlive());
    if (!message) {
        box.className = 'relay-check d-none';
        box.textContent = '';
        return;
    }

    box.className = 'relay-check relay-check--warning';
    box.innerHTML = '';

    const icon = document.createElement('i');
    icon.className = (typeof getNotificationIcon === 'function'
        ? getNotificationIcon('warning')
        : 'bi bi-exclamation-triangle-fill') + ' relay-check-icon';
    icon.setAttribute('aria-hidden', 'true');
    box.appendChild(icon);

    const text = document.createElement('span');
    text.textContent = message;
    box.appendChild(text);
}

/**
 * The reason the Settings form cannot be saved as it stands, for
 * handleSaveSettings() to show before it sends anything.
 * @returns {string} the reason, or '' when the form can be saved
 */
function relayKeepAliveBlocker() {
    const message = relayConstraintMessage(relayFormValues(), relayFormKeepAlive());
    renderRelayConstraint();
    return message;
}

// ===================== Firing a webhook by hand =====================

/**
 * Record whether the printer answered the last status check.
 *
 * Called by that check, which already runs on load, every 30 s and on the
 * refresh button. The confirmation before cutting mains power hangs off this
 * flag, and it must not depend on a request of its own: an extra call made at
 * the moment of the decision is one more thing that can fail, and its answer
 * would be no fresher than the one already on screen.
 *
 * @param {?boolean} reachable - true when the printer answered, false when it
 *   did not, null when the check itself failed and nothing was learned
 */
function relayNotePrinterReachable(reachable) {
    relayPrinterReachable = (reachable === null || reachable === undefined) ? null : !!reachable;
}

/**
 * Show the outcome of a hand-fired webhook in the column of the action that
 * fired it, in the same shape as every other status line here: no colour of its
 * own, the icon carries the severity.
 *
 * The line itself is one glance's worth (see relayFireOutcome). Anything longer
 * the server said about the same request is kept behind a toggle under it
 * rather than thrown away.
 *
 * @param {string} action - 'turn_on' | 'turn_off'
 * @param {string} message - text to show (empty hides the line)
 * @param {string} [kind] - 'info' | 'success' | 'warning' | 'error'
 * @param {string} [detail] - the server's own longer account, or '' for none
 */
function setRelayFireResult(action, message, kind = 'info', detail = '') {
    const el = document.getElementById(action === 'turn_off' ? 'relay-fire-result-off' : 'relay-fire-result-on');
    if (!el) return;

    el.textContent = '';
    el.className = 'relay-check relay-check--' + kind + (message ? '' : ' d-none');
    if (!message) return;

    const icon = document.createElement('i');
    icon.className = (typeof getNotificationIcon === 'function'
        ? getNotificationIcon(kind)
        : 'bi bi-info-circle-fill') + ' relay-check-icon';
    icon.setAttribute('aria-hidden', 'true');
    el.appendChild(icon);

    const body = document.createElement('div');
    body.className = 'relay-check-body';

    const text = document.createElement('span');
    text.textContent = message;
    body.appendChild(text);

    const extra = String(detail || '').trim();
    if (extra && extra !== message) {
        const panel = document.createElement('p');
        panel.className = 'relay-check-detail';
        panel.id = el.id + '-detail';
        panel.textContent = extra;
        panel.hidden = true;

        const chevron = document.createElement('i');
        chevron.className = 'bi bi-chevron-down relay-check-chevron';
        chevron.setAttribute('aria-hidden', 'true');

        const toggle = document.createElement('button');
        toggle.type = 'button';
        toggle.className = 'relay-check-more';
        toggle.setAttribute('aria-expanded', 'false');
        toggle.setAttribute('aria-controls', panel.id);
        toggle.appendChild(document.createTextNode('What the server said'));
        toggle.appendChild(chevron);
        toggle.addEventListener('click', () => {
            const opening = panel.hidden;
            panel.hidden = !opening;
            toggle.setAttribute('aria-expanded', opening ? 'true' : 'false');
            chevron.className = 'bi bi-chevron-' + (opening ? 'up' : 'down') + ' relay-check-chevron';
        });

        body.appendChild(toggle);
        body.appendChild(panel);
    }

    el.appendChild(body);
}

/**
 * Ask before cutting mains power, in the words of what is about to happen.
 *
 * Asked whenever the printer is answering, and also when nothing has been read
 * from it yet: the question is only worth asking when there is something to
 * lose, but "not known" is not the same as "nothing to lose".
 *
 * It is deliberately not asked when the last status check found the printer
 * silent. Cutting power to a device that is already off interrupts nothing, and
 * a dialog that appears every time, including the times it protects nothing,
 * is one people learn to click through before reading.
 *
 * @returns {Promise<boolean>} whether to go ahead
 */
async function relayAskToCut() {
    const message = relayPrinterReachable === true
        ? 'Cut mains power to the printer now? It is answering, so it is awake. A print or a ' +
          'feed in progress stops where it is, and the printer stays dead until a job wakes it ' +
          'or you send turn_on from here.'
        : 'Cut mains power to the printer now? Nothing has been read from the printer yet, so it ' +
          'may well be awake. A print or a feed in progress would stop where it is, and the ' +
          'printer stays dead until a job wakes it or you send turn_on from here.';

    if (typeof confirmDialog === 'function') {
        // The most destructive thing this app can do, and the only one that
        // reaches the hardware, so it is asked as a warning rather than in the
        // shape of every other question.
        return confirmDialog(message, {
            title: 'Cut mains power',
            confirmLabel: 'Cut mains power',
            destructive: true
        });
    }
    return window.confirm(message);
}

/**
 * The first sentence of a server message, which is where its reason lives: the
 * ones this reads open with what happened and go on to say what to do about it.
 *
 * A full stop only ends a sentence here when whitespace follows it, so the dots
 * in a host name or an address are not mistaken for one.
 *
 * @param {string} text
 * @returns {string} '' when there is nothing to take
 */
function relayFirstSentence(text) {
    const said = String(text || '').trim();
    if (!said) return '';
    const stop = said.search(/\.\s/);
    return stop === -1 ? said : said.slice(0, stop + 1);
}

/**
 * A server-supplied reason, but only when it can be read on its own line.
 *
 * The webhook URL is in the field directly above this line, so a reason that
 * names it again says nothing new and costs a wrapped line to read. Those are
 * left to the detail under the line, which carries the full text anyway. Any
 * URL disqualifies a reason, not only the one this request went to: a report
 * that could not name its destination (a 400, where nothing was sent) still
 * quotes the offending address in a few of its messages.
 *
 * @param {string} text - the reason as the server worded it
 * @param {string} url - the URL this request went to, as reported
 * @returns {string} '' when the reason carries a URL, or when there is none
 */
function relayReasonForLine(text, url) {
    const said = String(text || '').trim();
    if (!said) return '';
    if (url && said.indexOf(url) !== -1) return '';
    if (/https?:\/\//i.test(said)) return '';
    return said;
}

/**
 * Shorten a delivery failure to the clause that says what went wrong.
 * @param {string} error - the report's `error`
 * @returns {string}
 */
function relayShortReason(error) {
    return String(error || '').trim()
        .replace(/^Relay webhook /, '')
        .replace(/\.$/, '');
}

/**
 * What the mains supply was left in, as a clause. Only the three states the API
 * documents are reported; a build that says nothing gets no clause rather than
 * a guessed one.
 * @param {*} value - the report's `mains_power`
 * @returns {string} '' when there is nothing to say
 */
function relayMainsPhrase(value) {
    const state = String(value === undefined || value === null ? '' : value).trim().toLowerCase();
    if (state === 'on' || state === 'off' || state === 'unknown') return 'mains power ' + state;
    return '';
}

/**
 * Capitalise the first letter, so a clause can open a sentence.
 * @param {string} text
 * @returns {string}
 */
function relayCapitalize(text) {
    return text ? text.charAt(0).toUpperCase() + text.slice(1) : text;
}

/**
 * Keep the server's account whole: its message, plus the raw error when that is
 * not already quoted inside it.
 * @param {string} message
 * @param {*} error
 * @returns {string}
 */
function relayFireDetail(message, error) {
    const said = String(message || '').trim();
    const why = String(error || '').trim();
    if (!why) return said;
    if (!said) return why;
    return said.indexOf(why.replace(/\.$/, '')) === -1 ? said + ' ' + why : said;
}

/**
 * Put the outcome of a fired webhook into a line that a glance can take in, and
 * hand back the server's own longer account to sit behind it.
 *
 * The line is composed here from the report's structured fields rather than
 * taken from its `message`. That sentence opens by naming the webhook URL,
 * which is already in the field directly above the line, and runs to four
 * wrapped lines to carry three facts: whether it went out, what the relay
 * answered, and what the mains supply was left in. Those three are what the
 * payload reports, so they are what is shown.
 *
 * Nothing is discarded — the prose is returned as `detail`, because its caveat
 * (the printer is not probed here, so allow it a minute to boot) is worth
 * having. This is the opposite case to the hazard warning, which has one
 * authoritative wording and is shown verbatim: see renderRelayWarning().
 *
 * A failure has to stay legible, so the reason survives into the line: the HTTP
 * status the relay answered, or the transport error when nothing answered at
 * all, or — for a request the app refused to make — the sentence saying which
 * rule stopped it.
 *
 * @param {string} action - 'turn_on' | 'turn_off'
 * @param {{ok: boolean, status: number}} response
 * @param {*} data - the parsed body, or null
 * @returns {{line: string, detail: string, kind: string}}
 */
function relayFireOutcome(action, response, data) {
    const report = (data && typeof data === 'object') ? data : {};
    const said = String(report.message || report.detail || '').trim();
    const url = String(report.url || '').trim();
    const status = relayNumber(
        report.response_status !== undefined ? report.response_status :
        (report.status_code !== undefined ? report.status_code : report.http_status));

    // Refused before anything left the app: relay power control is off, no URL
    // is configured, the action is not one of the two. There is no status and
    // no mains state to report then, only which rule stopped it.
    if (!response.ok) {
        const why = relayReasonForLine(relayFirstSentence(said), url);
        return {
            line: action + ' was not sent. ' + (why || 'The app answered HTTP ' + response.status + '.'),
            detail: said,
            kind: 'error'
        };
    }

    // Delivered by us, refused by the relay: reported, never counted as a
    // webhook that worked.
    const delivered = report.success !== false;

    const facts = [];
    if (status !== null) {
        facts.push('HTTP ' + status);
    } else if (!delivered) {
        facts.push(relayReasonForLine(relayShortReason(report.error), url) || 'nothing came back');
    }
    const mains = relayMainsPhrase(report.mains_power);
    if (mains) facts.push(mains);

    return {
        line: action + (delivered ? ' delivered.' : ' not confirmed.') +
            (facts.length ? ' ' + relayCapitalize(facts.join(', ')) + '.' : ''),
        detail: relayFireDetail(said, report.error),
        kind: delivered ? 'success' : 'warning'
    };
}

/**
 * Fire one webhook now.
 *
 * The server sends it from the stored settings, so this makes no attempt to
 * push an unsaved edit along with it: what is being tested is the
 * configuration that the automatic webhooks will use.
 *
 * @param {string} action - 'turn_on' | 'turn_off'
 * @returns {Promise<boolean>} whether the webhook went out
 */
async function fireRelay(action) {
    if (relayFireInFlight || !relayFireSupported) return false;

    if (action === 'turn_off' && relayPrinterReachable !== false) {
        const go = await relayAskToCut();
        if (!go) {
            setRelayFireResult(action, 'Nothing was sent.', 'info');
            return false;
        }
    }

    const button = document.getElementById(action === 'turn_off' ? 'relay-fire-off' : 'relay-fire-on');
    const originalHtml = button ? button.innerHTML : '';
    relayFireInFlight = true;
    setRelayFireResult(action, '');
    if (button) {
        button.disabled = true;
        button.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Sending...';
    }

    try {
        const response = await fetch(RELAY_FIRE_ENDPOINT, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: action })
        });

        // The endpoint is newer than the rest of the feature, so a build without
        // it says so by not answering. That is not a failure to report: the
        // buttons simply withdraw, and everything else here still works.
        if (response.status === 404 || response.status === 405 || response.status === 501) {
            relayFireSupported = false;
            return false;
        }

        let data = null;
        try {
            data = await response.json();
        } catch (error) {
            // Non-JSON body; the status code still says how it went.
        }

        const outcome = relayFireOutcome(action, response, data);
        setRelayFireResult(action, outcome.line, outcome.kind, outcome.detail);

        // What the server reports about the relay has just moved: the last
        // action, or the last error.
        await refreshRelayStatus();
        return response.ok;
    } catch (error) {
        console.error('Error firing the relay webhook:', error);
        const why = String((error && error.message) || 'the request failed').replace(/\.$/, '');
        setRelayFireResult(action, action + ' was not sent. The app could not be reached: ' + why + '.', 'error');
        return false;
    } finally {
        relayFireInFlight = false;
        if (button) {
            button.disabled = false;
            button.innerHTML = originalHtml;
        }
        renderRelayFireButtons();
    }
}

/**
 * Reflect what the two test buttons can do, which depends on the STORED
 * settings rather than on the fields: the server fires what is in the file, so
 * a URL that has only been typed cannot be tested yet.
 */
function renderRelayFireButtons() {
    const on = document.getElementById('relay-fire-on');
    const off = document.getElementById('relay-fire-off');
    const note = document.getElementById('relay-fire-note');

    if (!relayFireSupported) {
        if (on) on.hidden = true;
        if (off) off.hidden = true;
        setRelayFireResult('turn_on', '');
        setRelayFireResult('turn_off', '');
        if (note) {
            note.textContent = 'This server build cannot fire a webhook on request, so there are ' +
                'no test buttons. The webhooks it sends by itself are unaffected.';
        }
        return;
    }

    const turnOn = String(relaySettings.turn_on_url || '').trim();
    const turnOff = String(relaySettings.turn_off_url || '').trim() || turnOn;
    const ready = !!relaySettings.enabled;

    const apply = (button, url) => {
        if (!button) return;
        button.hidden = false;
        button.disabled = relayFireInFlight || !ready || !url;
        button.title = button.disabled
            ? 'Save Settings with relay power control on and a turn-on URL first. The test sends the stored settings.'
            : '';
    };
    apply(on, turnOn);
    apply(off, turnOff);
}

/**
 * Which of the two answers the "Where it posts" choice is currently giving.
 * @returns {string} 'shared' | 'own'
 */
function relayTurnOffUrlMode() {
    const own = document.getElementById('relay-turn-off-url-own');
    return own && own.checked ? 'own' : 'shared';
}

/**
 * Read the relay fields into the same shape the settings document uses.
 *
 * An empty turn-off URL is how the settings document says "use the turn-on
 * one", so the shared answer is written that way and the field's contents are
 * simply not sent. Nothing new is stored for the choice itself.
 *
 * @returns {Object}
 */
function relayFormValues() {
    const enabledEl = document.getElementById('relay-enabled');
    const turnOnEl = document.getElementById('relay-turn-on-url');
    const turnOffEl = document.getElementById('relay-turn-off-url');
    const turnOffEnabledEl = document.getElementById('relay-turn-off-enabled');
    const delayEl = document.getElementById('relay-turn-off-delay');
    const hardwareEl = document.getElementById('relay-auto-power-off');

    const delay = parseInt(delayEl && delayEl.value, 10);
    const hardware = parseInt(hardwareEl && hardwareEl.value, 10);
    const mode = relayTurnOffUrlMode();

    return {
        enabled: !!enabledEl && enabledEl.value === 'true',
        turn_on_url: turnOnEl ? turnOnEl.value.trim() : '',
        turn_off_url_mode: mode,
        turn_off_url: (mode === 'own' && turnOffEl) ? turnOffEl.value.trim() : '',
        turn_off_enabled: !!turnOffEnabledEl && turnOffEnabledEl.value === 'true',
        turn_off_delay_minutes: Number.isFinite(delay) ? Math.min(60, Math.max(0, delay)) : 5,
        auto_power_off_minutes: RELAY_AUTO_POWER_OFF_CHOICES.indexOf(hardware) !== -1 ? hardware : 10
    };
}

/**
 * Push the stored settings into the fields. Called from applyRelaySettings(),
 * so the block is filled by the same load that fills the rest of Settings.
 */
function fillRelayFields() {
    const set = (id, value) => {
        const el = document.getElementById(id);
        if (el) el.value = value;
    };
    set('relay-enabled', relaySettings.enabled ? 'true' : 'false');
    set('relay-turn-on-url', relaySettings.turn_on_url || '');
    set('relay-turn-off-url', relaySettings.turn_off_url || '');
    set('relay-turn-off-enabled', relaySettings.turn_off_enabled ? 'true' : 'false');
    set('relay-turn-off-delay', String(relaySettings.turn_off_delay_minutes));
    set('relay-auto-power-off', String(relaySettings.auto_power_off_minutes));

    // A stored turn-off URL is the only thing that means "its own"; no URL is
    // the shared answer, which is also the default for a fresh configuration.
    const own = !!String(relaySettings.turn_off_url || '').trim();
    const ownRadio = document.getElementById('relay-turn-off-url-own');
    const sharedRadio = document.getElementById('relay-turn-off-url-shared');
    if (ownRadio) ownRadio.checked = own;
    if (sharedRadio) sharedRadio.checked = !own;

    updateRelayFields();
}

/**
 * Reflect what the fields say: the configuration appears with the master switch
 * and is not there at all while it is off, and the turn-off fields follow the
 * turn-off switch the same way. Everything that describes state rather than
 * configuration is repainted with it, because both the state block and the
 * constraint line read these fields.
 */
function updateRelayFields() {
    const values = relayFormValues();

    const details = document.getElementById('relay-details');
    if (details) details.hidden = !values.enabled;

    const turnOffFields = document.getElementById('relay-turn-off-fields');
    if (turnOffFields) turnOffFields.classList.toggle('relay-inert', !values.turn_off_enabled);

    // The second URL box exists only for the answer that asks for it.
    const turnOffUrl = document.getElementById('relay-turn-off-url');
    if (turnOffUrl) turnOffUrl.hidden = values.turn_off_url_mode !== 'own';

    renderRelayUI();
}

// ===================== Wiring =====================

/**
 * Wire up relay power control: the fields in Settings, the two test buttons and
 * the keep-alive controls the rules are shared with. Called from
 * setupEventListeners() in core.js.
 *
 * There is no save of its own. The fields are part of the Settings form, and
 * that form's Save carries them (see relaySettingsPatch), so the two rules are
 * checked once, against what is about to be sent.
 */
function setupRelayPower() {
    loadRelayWarning();

    // Every field re-runs the pre-check, so a combination the server would
    // refuse is named while it is being made rather than by a refused save.
    ['relay-enabled', 'relay-turn-on-url', 'relay-turn-off-url', 'relay-turn-off-enabled',
     'relay-turn-off-delay', 'relay-auto-power-off', 'relay-turn-off-url-shared',
     'relay-turn-off-url-own'].forEach(id => {
        const el = document.getElementById(id);
        if (!el) return;
        const event = (el.tagName === 'SELECT' || el.type === 'radio') ? 'change' : 'input';
        el.addEventListener(event, updateRelayFields);
    });

    // The two test buttons, each in the column of the action it fires.
    const fireOn = document.getElementById('relay-fire-on');
    if (fireOn) fireOn.addEventListener('click', () => { fireRelay('turn_on'); });

    const fireOff = document.getElementById('relay-fire-off');
    if (fireOff) fireOff.addEventListener('click', () => { fireRelay('turn_off'); });

    // The keep-alive controls are the other half of both rules, so the line
    // between them follows those too.
    ['keep-alive-enabled', 'keep-alive-mode', 'keep-alive-duration-value',
     'keep-alive-duration-unit'].forEach(id => {
        const el = document.getElementById(id);
        if (!el) return;
        const event = el.tagName === 'SELECT' ? 'change' : 'input';
        el.addEventListener(event, renderRelayConstraint);
    });

    // A hidden tab has nothing to repaint, and a countdown that ran while the
    // tab was in the background would only be caught up on the next read anyway.
    if (typeof document.addEventListener === 'function') {
        document.addEventListener('visibilitychange', () => {
            if (document.hidden) {
                stopRelayCountdown();
            } else {
                refreshRelayStatus();
            }
        });
    }

    updateRelayFields();
}
