// Brother QL Printer App - Relay power control
//
// The printer's mains supply can run through a relay driven over a webhook, so
// it draws nothing between print runs. Two events exist: "turn_on", fired when a
// print job arrives at a printer that is not answering, and "turn_off", fired
// once everything has wound down.
//
// The part a user has to understand is the timing, because three timers are
// involved and only two of them belong to this app:
//
//     0:00  last print
//     3:50  keep-alive heartbeat stops   (the configured window minus the
//                                         printer's own auto-power-off interval)
//     4:00  the printer powers itself off (exactly the window configured)
//     4:05  turn_off is sent              (after the safety margin, to a device
//                                          that is already off)
//
// None of those numbers are worked out here. GET /printers/relay-power returns
// the whole chain already derived, and this module only formats it — one place
// decides what the timing is, and it is the same place that acts on it.
//
// The same endpoint carries the warning about the printer's built-in
// auto-power-off, which the app can neither read nor change. That wording is
// shown verbatim rather than paraphrased, for the same reason: one source.

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
// is read from there rather than from the status endpoint so that the panel
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

// Bootstrap modal instance, created lazily on first open.
let relayModal = null;

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
 * loadSettings(), and again after every write this module makes.
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

    renderRelayUI();
}

/**
 * Record the keep-alive window that has just been stored by the Settings form.
 *
 * The relay's whole timing hangs off that window, and both rules the server
 * enforces compare the two — so a save of the form has to move the stored copy
 * here as well, or the dialog would go on checking against values that are no
 * longer in the file.
 *
 * @param {boolean} enabled - keep_alive_enabled as saved
 * @param {string} mode - keep_alive_mode as saved
 * @param {number} durationSeconds - keep_alive_duration_seconds as saved
 */
function relayNoteKeepAliveSaved(enabled, mode, durationSeconds) {
    relaySettings.keep_alive_enabled = !!enabled;
    relaySettings.keep_alive_mode = mode || 'forever';
    relaySettings.keep_alive_duration_seconds = relayNumber(durationSeconds) || 0;
    // The chain the server derives has moved with it.
    refreshRelayStatus();
    renderRelayUI();
}

/**
 * The relay settings this module owns, as a patch for the settings document.
 * @returns {Object}
 */
function relaySettingsPatch() {
    return {
        relay_webhook_enabled: relaySettings.enabled,
        relay_webhook_turn_on_url: relaySettings.turn_on_url,
        relay_webhook_turn_off_url: relaySettings.turn_off_url,
        relay_webhook_turn_off_enabled: relaySettings.turn_off_enabled,
        relay_webhook_turn_off_delay_minutes: relaySettings.turn_off_delay_minutes,
        printer_auto_power_off_minutes: relaySettings.auto_power_off_minutes
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
 * Repaint just the countdown numbers, in both places they appear. Nothing else
 * in the panel changes between server reads, so nothing else is touched.
 */
function paintRelayCountdown() {
    const remaining = relayRemainingMs();
    const text = remaining === null ? '' : formatRelayCountdown(remaining / 1000);
    ['relay-countdown', 'relay-countdown-live'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.textContent = text;
    });
}

// ===================== Rendering =====================

/**
 * One line saying what state the feature is in, used by both the Settings
 * summary and the dialog's banner.
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
 * Paint the Settings summary: the state, the countdown to the next turn-off and
 * the outcome of the last webhook. It is the whole of the feature that is
 * visible without opening the dialog, so it says what is happening rather than
 * what is configured.
 */
function renderRelaySummary() {
    const box = document.getElementById('relay-summary');
    if (!box) return;

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
                    '<span>Nothing is scheduled — the window starts at the next print.</span>' +
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

    box.innerHTML = rows.join('');
}

/**
 * Paint the timing chain: the four moments, counted from the last print.
 *
 * Every number in it comes from the status endpoint, which derives the whole
 * chain from the same settings the service acts on. Nothing is recomputed here
 * — the only arithmetic is adding two of the server's own numbers together for
 * the last row, because the moment it names is the sum of a window and a margin
 * the endpoint reports separately.
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

    const rows = [row(0, 'Last print', 'The window starts here, and any new job starts it again.')];

    if (effective === null) {
        rows.push(row(0, 'The heartbeat runs continuously', 'Keep Alive is not timed.'));
    } else if (effective === 0) {
        rows.push(row(0, 'The keep-alive heartbeat does nothing',
            'The window is exactly the printer\'s own interval, so the hardware carries all of it.',
            'is-quiet'));
    } else {
        rows.push(row(effective, 'The keep-alive heartbeat stops',
            'The ' + formatRelayDuration(window_) + ' window minus the printer\'s own ' +
            (hardware === null ? 'interval' : hardware + ' min') + '.'));
    }

    rows.push(row(window_, 'The printer powers itself off',
        'Its own timer, started when the heartbeat stopped — exactly the window configured.'));

    if (relaySettings.turn_off_enabled && delay !== null) {
        rows.push(row(window_ + delay, 'turn_off is sent',
            'After a ' + formatRelayDuration(delay) + ' safety margin, to a printer that should ' +
            'already be off.', 'is-cut'));
    } else {
        rows.push(
            '<li class="relay-chain-row is-quiet">' +
                '<span class="relay-chain-time mono">—</span>' +
                '<span class="relay-chain-body">' +
                    '<span class="relay-chain-what">turn_off is not sent</span>' +
                    '<span class="relay-chain-why">Mains power stays on; the printer sleeps on its own.</span>' +
                '</span>' +
            '</li>'
        );
    }

    list.innerHTML = rows.join('');

    if (note) {
        note.textContent = 'Counted from the last print, as h:mm. These are the stored settings ' +
            'as the server reads them — save to see an edit reflected here.';
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
 * Paint everything the relay owns: the Settings summary, the dialog's fields
 * that mirror server state, the chain, the warning and the countdown timer.
 */
function renderRelayUI() {
    renderRelaySummary();
    renderRelayChain();
    renderRelayWarning();
    renderRelayLive();
    renderRelayConstraint();

    if (relayRemainingMs() !== null) {
        startRelayCountdown();
    } else {
        stopRelayCountdown();
    }
}

/**
 * The dialog's live block: the countdown, the last webhook and the last error.
 * Deliberately separate from the form above it — one says what is configured,
 * the other says what has actually happened.
 */
function renderRelayLive() {
    const box = document.getElementById('relay-live');
    if (!box) return;

    if (!relaySettings.enabled) {
        box.innerHTML = '';
        box.hidden = true;
        return;
    }
    box.hidden = false;

    const rows = [];
    const remaining = relayRemainingMs();

    if (remaining !== null && remaining > 0) {
        const at = new Date(Date.now() + remaining);
        rows.push(
            '<div class="relay-live-row">' +
                '<span class="relay-live-key">Mains power off in</span>' +
                '<span class="relay-live-value"><span class="relay-countdown mono" id="relay-countdown-live">' +
                    escapeHtml(formatRelayCountdown(remaining / 1000)) + '</span>' +
                    ' <span class="relay-live-at">(' + escapeHtml(formatRelayMoment(at.toISOString())) + ')</span></span>' +
            '</div>'
        );
    } else if (relaySettings.turn_off_enabled) {
        rows.push(
            '<div class="relay-live-row">' +
                '<span class="relay-live-key">Mains power off in</span>' +
                '<span class="relay-live-value relay-live-value--idle">nothing scheduled</span>' +
            '</div>'
        );
    }

    const action = relayStatus && relayStatus.last_action;
    rows.push(
        '<div class="relay-live-row">' +
            '<span class="relay-live-key">Last webhook</span>' +
            '<span class="relay-live-value">' + (action
                ? '<span class="mono">' + escapeHtml(String(action)) + '</span>' +
                  (formatRelayMoment(relayStatus.last_action_at)
                      ? ' <span class="relay-live-at">' + escapeHtml(formatRelayMoment(relayStatus.last_action_at)) + '</span>'
                      : '')
                : '<span class="relay-live-value--idle">none sent yet</span>') +
            '</span>' +
        '</div>'
    );

    const error = relayStatus && relayStatus.last_error;
    if (error) {
        rows.push(
            '<div class="relay-live-row relay-live-row--error">' +
                '<span class="relay-live-key">' +
                    '<i class="bi bi-exclamation-circle-fill relay-icon-error" aria-hidden="true"></i> Last failure' +
                '</span>' +
                '<span class="relay-live-value">' + escapeHtml(String(error)) +
                    (formatRelayMoment(relayStatus.last_error_at)
                        ? ' <span class="relay-live-at">' + escapeHtml(formatRelayMoment(relayStatus.last_error_at)) + '</span>'
                        : '') +
                '</span>' +
            '</div>'
        );
    }

    if (relayStatus && relayStatus.authorization_configured) {
        rows.push(
            '<div class="relay-live-row">' +
                '<span class="relay-live-key">Authorization</span>' +
                '<span class="relay-live-value">sent from the environment</span>' +
            '</div>'
        );
    }

    box.innerHTML = rows.join('');
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
        return 'A turn-on webhook URL is required while relay power control is on — ' +
            'there is nothing to call to switch the printer on.';
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

    return '';
}

/**
 * Whether the Settings form's keep-alive controls still say what is stored. The
 * dialog checks against the stored values, because those are the ones the
 * server will validate a relay write against — so a pending edit in the form
 * has to be named rather than silently used or silently ignored.
 * @returns {boolean}
 */
function relayKeepAliveIsUnsaved() {
    const form = relayFormKeepAlive();
    return form.enabled !== relaySettings.keep_alive_enabled ||
        form.mode !== relaySettings.keep_alive_mode ||
        form.duration !== relaySettings.keep_alive_duration_seconds;
}

/**
 * Paint the constraint line in the Settings panel: what would stop the next
 * save of that form, before it is attempted.
 *
 * It exists because the two rules are enforced on the settings document as a
 * whole: switching Keep Alive to "Forever" while the relay is armed to cut
 * power is refused, and the refusal would otherwise arrive as a failed save of
 * everything else in the form as well.
 */
function renderRelayConstraint() {
    const box = document.getElementById('relay-constraint');
    if (!box) return;

    const message = relayConstraintMessage(relaySettings, relayFormKeepAlive());
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
    const message = relayConstraintMessage(relaySettings, relayFormKeepAlive());
    renderRelayConstraint();
    return message;
}

// ===================== The dialog =====================

/**
 * Show a status line inside the dialog. Carries no colour of its own; the icon
 * says how severe it is, the same way the notifications do.
 * @param {string} message - text to show (empty hides the line)
 * @param {string} [kind] - 'info' | 'success' | 'warning' | 'error'
 */
function setRelayCheck(message, kind = 'info') {
    const el = document.getElementById('relay-check');
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

    const text = document.createElement('span');
    text.textContent = message;
    el.appendChild(text);
}

/**
 * Read the dialog's fields into the same shape the settings document uses.
 * @returns {Object}
 */
function relayDialogValues() {
    const enabledEl = document.getElementById('relay-enabled');
    const turnOnEl = document.getElementById('relay-turn-on-url');
    const turnOffEl = document.getElementById('relay-turn-off-url');
    const turnOffEnabledEl = document.getElementById('relay-turn-off-enabled');
    const delayEl = document.getElementById('relay-turn-off-delay');
    const hardwareEl = document.getElementById('relay-auto-power-off');

    const delay = parseInt(delayEl && delayEl.value, 10);
    const hardware = parseInt(hardwareEl && hardwareEl.value, 10);

    return {
        enabled: !!enabledEl && enabledEl.value === 'true',
        turn_on_url: turnOnEl ? turnOnEl.value.trim() : '',
        turn_off_url: turnOffEl ? turnOffEl.value.trim() : '',
        turn_off_enabled: !!turnOffEnabledEl && turnOffEnabledEl.value === 'true',
        turn_off_delay_minutes: Number.isFinite(delay) ? Math.min(60, Math.max(0, delay)) : 5,
        auto_power_off_minutes: RELAY_AUTO_POWER_OFF_CHOICES.indexOf(hardware) !== -1 ? hardware : 10
    };
}

/**
 * Push the stored settings into the dialog's fields.
 */
function fillRelayDialog() {
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
    updateRelayDialogUI();
}

/**
 * Reflect the dialog's own state: the fields below the master switch are inert
 * while it is off, exactly as the settings are, and the turn-off fields follow
 * the turn-off switch the same way.
 */
function updateRelayDialogUI() {
    const values = relayDialogValues();

    const details = document.getElementById('relay-details');
    if (details) details.hidden = !values.enabled;

    const turnOffFields = document.getElementById('relay-turn-off-fields');
    if (turnOffFields) turnOffFields.classList.toggle('relay-inert', !values.turn_off_enabled);

    const banner = document.getElementById('relay-state-flag');
    const detail = document.getElementById('relay-state-detail');
    const state = relayStateLine();
    if (banner) {
        banner.textContent = state.flag;
        banner.className = 'relay-state-flag relay-state--' + state.tone;
    }
    if (detail) detail.textContent = state.detail;

    // The pre-check runs on every edit, so the two rules are answered while the
    // combination is being made rather than by a refused save.
    const message = relayConstraintMessage(values, {
        enabled: relaySettings.keep_alive_enabled,
        mode: relaySettings.keep_alive_mode,
        duration: relaySettings.keep_alive_duration_seconds
    });

    if (message) {
        setRelayCheck(message, 'warning');
    } else if (values.enabled && relayKeepAliveIsUnsaved()) {
        setRelayCheck('Keep Alive has unsaved changes in Settings. The check here uses the stored ' +
            'values, which are the ones the server checks a relay change against.', 'info');
    } else {
        setRelayCheck('');
    }

    const save = document.getElementById('relay-save');
    if (save) save.disabled = !!message;
}

/**
 * Open the relay dialog, on the stored settings and the freshest status.
 */
function openRelayDialog() {
    const modalEl = document.getElementById('relayModal');
    if (!modalEl) return;

    fillRelayDialog();
    renderRelayUI();
    // The dialog is the one place the countdown and the last error are read
    // closely, so it is worth one request on opening rather than showing
    // whatever the 30 s poll last left behind.
    refreshRelayStatus();

    if (!relayModal && window.bootstrap && bootstrap.Modal) {
        relayModal = new bootstrap.Modal(modalEl);
    }
    if (relayModal) relayModal.show();
}

/**
 * Write the relay settings.
 *
 * Only the relay keys are sent, on top of a freshly read settings document, so
 * an unsaved edit elsewhere in the Settings form is never committed as a side
 * effect — the same shape the media and calibration writes use.
 */
async function saveRelaySettings() {
    const button = document.getElementById('relay-save');
    const values = relayDialogValues();

    const message = relayConstraintMessage(values, {
        enabled: relaySettings.keep_alive_enabled,
        mode: relaySettings.keep_alive_mode,
        duration: relaySettings.keep_alive_duration_seconds
    });
    if (message) {
        setRelayCheck(message, 'warning');
        return false;
    }

    const originalHtml = button ? button.innerHTML : '';
    if (button) {
        button.disabled = true;
        button.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Saving...';
    }

    // Whether the half that can cut mains power is being switched on here. The
    // server only issues its warning once that is stored, so this is the moment
    // to make sure it is read.
    const armingTurnOff = values.turn_off_enabled && !relaySettings.turn_off_enabled;

    try {
        let base = null;
        try {
            const read = await fetch('/api/v1/settings');
            if (read.ok) base = await read.json();
        } catch (error) {
            console.error('Error reading settings for the relay:', error);
        }

        const uriEl = document.getElementById('printer-uri');
        const modelEl = document.getElementById('printer-model');
        const labelEl = document.getElementById('label-size');

        const body = {
            printer_uri: (base && base.printer_uri) || (uriEl ? uriEl.value : ''),
            printer_model: (base && base.printer_model) || (modelEl ? modelEl.value : ''),
            label_size: (base && base.label_size) || (labelEl ? labelEl.value : ''),
            relay_webhook_enabled: values.enabled,
            relay_webhook_turn_on_url: values.turn_on_url,
            relay_webhook_turn_off_url: values.turn_off_url,
            relay_webhook_turn_off_enabled: values.turn_off_enabled,
            relay_webhook_turn_off_delay_minutes: values.turn_off_delay_minutes,
            printer_auto_power_off_minutes: values.auto_power_off_minutes
        };

        const response = await fetch('/api/v1/settings', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });

        if (!response.ok) {
            let reason = 'Error: ' + response.status;
            try {
                const data = await response.json();
                reason = data.message || data.details || reason;
            } catch (error) {
                // Non-JSON body: keep the generic message.
            }
            throw new Error(reason);
        }

        // Take the server's view of what is now stored, rather than the form's.
        applyRelaySettings(Object.assign({}, base || {}, body));
        await refreshRelayStatus();
        setRelayCheck('Saved.', 'success');
        if (typeof showNotification === 'function') {
            showNotification(values.enabled
                ? 'Relay power control saved.'
                : 'Relay power control switched off and saved.', 'success');
        }

        if (armingTurnOff && relayWarningText && typeof showNotification === 'function') {
            // Not a second copy of the wording: the same string, said once more
            // at the moment the thing it warns about becomes possible.
            showNotification(relayWarningText, 'warning', 15000);
        }
        return true;
    } catch (error) {
        console.error('Error saving the relay settings:', error);
        setRelayCheck('Could not save: ' + error.message, 'error');
        return false;
    } finally {
        if (button) {
            button.disabled = false;
            button.innerHTML = originalHtml;
        }
        updateRelayDialogUI();
    }
}

// ===================== Wiring =====================

/**
 * Wire up relay power control: the Settings summary, the dialog's fields and
 * its save. Called from setupEventListeners() in core.js.
 */
function setupRelayPower() {
    loadRelayWarning();

    const open = document.getElementById('relay-open');
    if (open) open.addEventListener('click', openRelayDialog);

    const save = document.getElementById('relay-save');
    if (save) save.addEventListener('click', saveRelaySettings);

    // Every field in the dialog re-runs the pre-check, so a combination the
    // server would refuse is named while it is being made.
    ['relay-enabled', 'relay-turn-on-url', 'relay-turn-off-url', 'relay-turn-off-enabled',
     'relay-turn-off-delay', 'relay-auto-power-off'].forEach(id => {
        const el = document.getElementById(id);
        if (!el) return;
        const event = el.tagName === 'SELECT' ? 'change' : 'input';
        el.addEventListener(event, updateRelayDialogUI);
    });

    // The keep-alive controls are the other half of both rules, so the Settings
    // panel's own line follows them.
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

    renderRelayUI();
}
