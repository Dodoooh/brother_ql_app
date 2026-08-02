// Brother QL Printer App - Loaded-media detection
//
// The printer can say which roll is in it. This module turns that report into
// three surfaces:
//
//   * a pill in the top bar that says what is loaded and whether the app agrees
//     with it, and that opens the label picker so the roll can be changed from
//     the header;
//   * the picker's "Loaded in the printer" group (labels.js renders it, this
//     module feeds it);
//   * a warning next to the label picker when the configured label type is not
//     what the printer has.
//
// Nothing here polls. checkPrinterStatus() in api.js already asks the server
// every 30 s and hands the response over.

// ===================== The wire format =====================
//
// This is the ONLY place the shape of the server's answer is read. The backend
// is being written against the same contract, so if a field is renamed, it is
// renamed here and nowhere else.
//
// Shape of POST /api/v1/printers/status:
//
//   {
//     "available": true,
//     "reachable": true,
//     "state": "ready",
//     "blocking_reasons": ["cover-open"],
//     "status": "Ready",
//     "media": {
//       "width_mm": 62, "length_mm": null, "media_type": …, "is_round": …,
//       "detected": true,
//       "detection": "ok" | "no-media" | "unidentified" | "unreachable" | "unsupported",
//       "candidates": ["62", "62red"],   // label_size identifiers
//       "ambiguous": true,
//       "reason": "…",                   // why there is more than one, or none
//       "label_size": "62",
//       "matches_label_size": true | false | null,
//
//       // Which single identifier the candidates come down to, and how. Sent
//       // whether or not automatic switching is on, because narrowing is
//       // useful to a client that only wants to *offer* the switch.
//       "resolution": {
//         "label_size": "62red" | null,
//         "resolved_by": "detection" | "memory" | "owned" | "default" | null,
//         "reason": "…"
//       },
//       // What automatic switching wants done. The server decides and the
//       // client applies: label_size has exactly one writer, and a status
//       // poll that wrote it would race the user's own saves.
//       "auto_switch": {
//         "enabled": true, "action": "none" | "switch" | "ambiguous",
//         "from": "12", "to": "62red" | null, "reason": "…"
//       }
//     }
//   }
//
// Everything here is read defensively: every field is treated as optional, the
// container is also accepted under `loaded_media` or nested in `details`, a
// `state` key is honoured in place of `detection`, and candidates may arrive as
// plain strings or as objects carrying an `identifier` / `label_size` / `value`.
// A response with none of it (any server that predates the feature) parses to
// the "unknown" state, which is exactly how the UI looked before - and with none
// of the resolution fields, the automatic switch falls back to deciding in the
// browser from the same inputs the server would have used.
//
// A resolution is only believed when it names a candidate that is actually in
// the list and that the picker can offer. That makes it impossible for a
// renamed or misread field to move the label type: anything that is not one of
// the identifiers already on the table is dropped, and the browser decides.
//
// `matches_label_size` is deliberately NOT used: the server compares against the
// stored settings, while the UI must agree with #label-size, which is the single
// source of truth in the browser. The comparison is redone here against that.

/** The state used whenever nothing can be said. Shared, so never mutated. */
const MEDIA_NOTHING_KNOWN = Object.freeze({
    state: 'unknown',
    candidates: Object.freeze([]),
    reason: '',
    coverOpen: false,
    noMedia: false,
    widthMm: null,
    resolved: '',
    resolvedBy: '',
    resolvedReason: '',
    autoAction: '',
    autoTo: '',
    autoReason: '',
    narrowedByOwned: false
});

/** `detection` values that mean the printer answered and has nothing loaded. */
const MEDIA_STATES_EMPTY = ['none', 'empty', 'no-media', 'no_media', 'nomedia', 'not_loaded'];

/**
 * Pick the first present, non-null property out of several candidate names.
 * @param {Object} source - object to read
 * @param {string[]} names - property names, in order of preference
 * @returns {*} the first value that is neither undefined nor null
 */
function firstDefined(source, names) {
    if (!source || typeof source !== 'object') return undefined;
    for (let i = 0; i < names.length; i++) {
        const value = source[names[i]];
        if (value !== undefined && value !== null) return value;
    }
    return undefined;
}

/**
 * Reduce one entry of the candidate list to a label type identifier.
 * @param {*} item - a string, or an object carrying the identifier
 * @returns {string} the identifier, or an empty string
 */
function candidateIdentifier(item) {
    if (typeof item === 'string') return item.trim();
    const value = firstDefined(item, ['identifier', 'label_size', 'labelSize', 'value', 'id']);
    return typeof value === 'string' ? value.trim() : '';
}

/**
 * Read the loaded-media report out of a printer status response.
 *
 * Defensive by construction: any shape it does not recognise, and any value it
 * cannot use, degrades to "nothing known" rather than throwing. Callers can
 * pass null, a string, or a half-built object without special-casing.
 *
 * @param {*} data - the parsed body of POST /printers/status
 * @returns {{state: string, candidates: string[], reason: string,
 *   coverOpen: boolean, noMedia: boolean, widthMm: ?number, resolved: string,
 *   resolvedBy: string, resolvedReason: string, narrowedByOwned: boolean}}
 */
function parseLoadedMedia(data) {
    try {
        if (!data || typeof data !== 'object') return MEDIA_NOTHING_KNOWN;

        const details = (data.details && typeof data.details === 'object') ? data.details : {};
        const container = firstDefined(data, ['media', 'loaded_media', 'loadedMedia']) ||
            firstDefined(details, ['media', 'loaded_media', 'loadedMedia']) ||
            details;
        if (!container || typeof container !== 'object') return MEDIA_NOTHING_KNOWN;

        const rawCandidates = firstDefined(container,
            ['candidates', 'label_sizes', 'labelSizes', 'label_size_candidates', 'matches']);
        let candidates = [];
        if (Array.isArray(rawCandidates)) {
            candidates = rawCandidates.map(candidateIdentifier).filter(id => id.length > 0);
        } else {
            // A printer that identifies the roll outright may report the single
            // identifier rather than a list of one.
            const single = candidateIdentifier(firstDefined(container,
                ['label_size', 'labelSize', 'identifier']));
            if (single) candidates = [single];
        }
        // A candidate the picker cannot offer is worse than no candidate: it
        // would show a row that writes an invalid value into #label-size.
        candidates = candidates.filter(id => typeof labelEntryFor === 'function' && labelEntryFor(id));
        // Same identifier twice (however the server got there) reads as an
        // ambiguity that is not one.
        candidates = candidates.filter((id, index) => candidates.indexOf(id) === index);

        const coverOpen = truthy(firstDefined(container, ['cover_open', 'coverOpen'])) ||
            truthy(firstDefined(details, ['cover_open', 'coverOpen'])) ||
            reportsCoverOpen(data) || reportsCoverOpen(details);
        const noMedia = truthy(firstDefined(container, ['no_media', 'noMedia', 'media_empty', 'empty'])) ||
            truthy(firstDefined(details, ['no_media', 'noMedia']));

        const rawState = firstDefined(container, ['detection', 'state', 'media_state', 'mediaState']);
        const declared = (typeof rawState === 'string') ? rawState.trim().toLowerCase() : '';

        let state;
        if (candidates.length === 1) {
            state = 'identified';
        } else if (candidates.length > 1) {
            state = 'ambiguous';
        } else if (noMedia || MEDIA_STATES_EMPTY.indexOf(declared) !== -1) {
            state = 'none';
        } else {
            // "unidentified" (a medium this app has no label type for),
            // "unreachable", "unsupported" and anything unrecognised all mean
            // the same thing to the UI: nothing to act on.
            state = 'unknown';
        }

        const rawReason = firstDefined(container, ['reason', 'ambiguous_reason', 'note', 'detail']);
        const rawWidth = firstDefined(container, ['width_mm', 'widthMm', 'media_width_mm']);
        const width = Number(rawWidth);

        // Which candidate the server settled on, if it settled on one. Believed
        // only when it names one of the candidates above, so nothing the server
        // could rename or mis-send is able to move the label type on its own.
        const resolution = firstDefined(container, ['resolution', 'media_resolution', 'mediaResolution']);
        const resolutionBox = (resolution && typeof resolution === 'object') ? resolution : container;
        const rawResolved = candidateIdentifier(firstDefined(resolutionBox, [
            'label_size', 'labelSize', 'resolved_label_size', 'resolvedLabelSize', 'resolved'
        ]));
        const resolved = (rawResolved && candidates.indexOf(rawResolved) !== -1) ? rawResolved : '';

        const rawResolvedBy = firstDefined(resolutionBox, ['resolved_by', 'resolvedBy']);
        const rawResolvedReason = firstDefined(resolutionBox,
            ['reason', 'resolution_reason', 'resolutionReason']);

        // What the server would like done about it. Read for the same reason
        // the resolution is: it was computed from the same stored settings, and
        // one decision made in one place cannot drift from itself.
        const auto = firstDefined(container, ['auto_switch', 'autoSwitch']);
        const autoBox = (auto && typeof auto === 'object') ? auto : {};
        const rawAction = firstDefined(autoBox, ['action']);
        const action = (typeof rawAction === 'string') ? rawAction.trim().toLowerCase() : '';
        const rawTo = candidateIdentifier(firstDefined(autoBox, ['to', 'label_size', 'labelSize']));
        const rawAutoReason = firstDefined(autoBox, ['reason']);

        return {
            state: state,
            candidates: candidates,
            reason: (typeof rawReason === 'string') ? rawReason.trim() : '',
            coverOpen: coverOpen,
            noMedia: noMedia,
            widthMm: Number.isFinite(width) && width > 0 ? width : null,
            resolved: resolved,
            resolvedBy: (resolved && typeof rawResolvedBy === 'string') ? rawResolvedBy.trim() : '',
            resolvedReason: (resolved && typeof rawResolvedReason === 'string') ? rawResolvedReason.trim() : '',
            autoAction: (action === 'switch' || action === 'ambiguous' || action === 'none') ? action : '',
            autoTo: (rawTo && candidates.indexOf(rawTo) !== -1) ? rawTo : '',
            autoReason: (typeof rawAutoReason === 'string') ? rawAutoReason.trim() : '',
            narrowedByOwned: false
        };
    } catch (error) {
        console.warn('Could not read the loaded-media report:', error);
        return MEDIA_NOTHING_KNOWN;
    }
}

/**
 * Does a response say the cover is open?
 *
 * A printer with its cover open answers happily and reports it as a blocking
 * state reason rather than as a media flag, so both places are read.
 * @param {*} source - the response, or its details object
 * @returns {boolean}
 */
function reportsCoverOpen(source) {
    const reasons = firstDefined(source,
        ['blocking_reasons', 'blockingReasons', 'printer_state_reasons', 'printerStateReasons']);
    // IPP reports these as a list, but a server that passes the attribute
    // straight through sends the single-reason case as a bare string.
    const list = Array.isArray(reasons) ? reasons : (typeof reasons === 'string' ? reasons.split(/[,\s]+/) : []);
    return list.some(reason => typeof reason === 'string' &&
        reason.toLowerCase().replace(/[^a-z]+/g, '').indexOf('coveropen') === 0);
}

/**
 * Loose truthiness for a flag that may arrive as a boolean, a number or a
 * string ("true", "1", "yes").
 * @param {*} value
 * @returns {boolean}
 */
function truthy(value) {
    if (typeof value === 'boolean') return value;
    if (typeof value === 'number') return value !== 0;
    if (typeof value === 'string') {
        const text = value.trim().toLowerCase();
        return text === 'true' || text === '1' || text === 'yes' || text === 'open';
    }
    return false;
}

// ===================== State =====================

// The current report, narrowed to what the user could actually have loaded.
let loadedMedia = MEDIA_NOTHING_KNOWN;

// The same report exactly as the printer gave it. Kept so that editing the
// owned-media list re-decides the current roll rather than only the next one.
let reportedMedia = MEDIA_NOTHING_KNOWN;

// The last report that actually identified something, kept for the whole
// session. It is what lets the pill say "was 24 mm round" while a roll is being
// swapped, instead of losing its identity the moment the cover opens.
let lastSeenMedia = null;

// The candidate set the user has already been told about, so a swap is
// announced once rather than on every poll. null until the first report.
let announcedMediaKey = null;

// One-shot re-checks scheduled while a roll is being changed (see
// scheduleMediaRecheck).
let mediaRecheckTimers = [];

// ===================== Automatic switching =====================
//
// Three stored settings, mirrored here and written back through the same
// partial settings PUT the label type uses:
//
//   media_auto_switch  boolean  follow the printer without being asked
//   owned_media        string[] label identifiers the user actually has
//   media_memory       object   medium -> the identifier last settled on for it
//
// Off by default, because it changes a stored setting with no user action.
//
// The order an ambiguity is decided in is the whole safety argument. Detection
// pins all fifteen die-cut sizes on its own; 62/62red, 12/12+17 and 103/104 it
// cannot, and 62red is the one where a wrong guess prints a bad label rather
// than failing.
//
// The server decides and this decides nothing twice: it holds the same three
// settings and runs memory, then the owned list, then the documented plain
// variant, and sends back both what the medium comes down to and whether to
// act on it. All that is done here is carrying it out - and checking that what
// came back is one of the candidates already on the table, so no field the
// server could rename or mis-send can move the label type.
//
// The one thing decided here is what to do on a server that sends none of it:
// narrow by the owned list, then recall what was last settled on, and stop. The
// plain-variant default is deliberately NOT reinvented in the browser - it is a
// documented rule of the backend's, and an old server that has never heard of
// any of this should leave the user asked rather than have the page guess
// between 62 and 62red on its behalf.

/** Whether the app follows the printer by itself. */
let mediaAutoSwitch = false;

/** Label identifiers the user says they own; empty means "no list given". */
let ownedMedia = [];

/** Medium key -> the identifier last settled on for it. */
let mediaMemory = {};

/**
 * The label identifiers the printer genuinely cannot tell apart, keyed by the
 * medium's plain variant.
 *
 * These are properties of the media rather than of any one printer: 62 and
 * 62red are the same geometry and the device reports no colour, 12 and 12+17
 * are one physical roll addressed with two raster widths, and 103 and 104
 * differ by less than the reported resolution.
 *
 * The key is the plain variant and not the candidate list, because the key has
 * to survive the catalogue: a new variant of the 62 mm roll, or a reordering of
 * the table, would orphan every entry filed under a list. "62" means the 62 mm
 * roll however many identifiers it grows. This is the same key the stored
 * media_memory uses, so what is written here is what the server validates.
 */
const MEDIA_VARIANT_GROUPS = {
    '62': ['62', '62red'],
    '12': ['12', '12+17'],
    '103': ['103', '104']
};

/**
 * The medium an identifier belongs to, as the key media_memory is filed under.
 * @param {string} identifier - a label type identifier
 * @returns {string} the medium key, or an empty string for media that are not
 *   ambiguous and so have nothing to remember
 */
function mediaMemoryKeyFor(identifier) {
    const keys = Object.keys(MEDIA_VARIANT_GROUPS);
    for (let i = 0; i < keys.length; i++) {
        if (MEDIA_VARIANT_GROUPS[keys[i]].indexOf(identifier) !== -1) return keys[i];
    }
    return '';
}

/**
 * What was last settled on for the medium these candidates describe.
 * @param {string[]} candidates - the candidate identifiers
 * @returns {string} the remembered identifier, or '' when there is none that is
 *   still on the table
 */
function rememberedChoiceFor(candidates) {
    for (let i = 0; i < candidates.length; i++) {
        const key = mediaMemoryKeyFor(candidates[i]);
        const remembered = key ? mediaMemory[key] : '';
        if (remembered && candidates.indexOf(remembered) !== -1) return remembered;
    }
    return '';
}

/**
 * Why the printer cannot tell two label types apart.
 *
 * The three ambiguous cases are fixed properties of the media, so the wording
 * is written out here rather than left to whatever the server sends; a server
 * reason is used for anything else, and a plain sentence covers the rest.
 */
const MEDIA_AMBIGUITY_REASONS = {
    '62|62red': 'The printer reports 62 mm continuous but not its colour, so the ' +
        'plain paper roll and the black/red roll look identical to it. Which one ' +
        'is loaded is something only you can say.',
    '12|12+17': 'Both address the same 12 mm roll and differ only in how wide a ' +
        'raster is sent to the printer. Prefer 12 mm unless the printer rejects it.',
    '103|104': 'The same 103.6 mm roll under two identifiers, differing only in the ' +
        'raster offset the printer model expects.'
};

/**
 * A stable key for a candidate set, used to compare reports.
 * @param {?Object} info - a parsed media report
 * @returns {string}
 */
function mediaKey(info) {
    return (info && info.candidates.length > 0) ? info.candidates.slice().sort().join('|') : '';
}

/**
 * The reason to show beside an ambiguous detection.
 * @param {Object} info - a parsed media report
 * @returns {string} the reason, or an empty string when there is nothing to say
 */
function mediaAmbiguityReason(info) {
    if (!info || info.candidates.length < 2) return '';
    const known = MEDIA_AMBIGUITY_REASONS[mediaKey(info)];
    if (known) return known;
    if (info.reason) return info.reason;
    return 'The printer cannot tell these label types apart; pick the roll you loaded.';
}

/**
 * A human name for what is loaded.
 *
 * One candidate is simply its catalogue name. Several candidates that share a
 * width and a form are named by what the printer actually knows ("62 mm
 * continuous") rather than by picking one of them.
 *
 * @param {?Object} info - a parsed media report
 * @returns {string} the name, or an empty string when nothing is detected
 */
function mediaDisplayName(info) {
    if (!info || info.candidates.length === 0) return '';

    const entries = info.candidates
        .map(id => (typeof labelEntryFor === 'function' ? labelEntryFor(id) : null))
        .filter(entry => entry);
    if (entries.length === 0) return info.candidates.join(' or ');
    if (entries.length === 1) return entries[0].name;

    const sameWidth = entries.every(entry => entry.width_mm === entries[0].width_mm);
    const sameForm = entries.every(entry => entry.form === entries[0].form);
    if (sameWidth && sameForm) {
        const form = entries[0].form === 'continuous' ? 'continuous'
            : (entries[0].form === 'round-die-cut' ? 'round' : 'die-cut');
        return entries[0].width_mm + ' mm ' + form;
    }
    return entries.map(entry => entry.name).join(' or ');
}

/**
 * The name of the label type the app is currently set to print for.
 * @returns {string}
 */
function configuredLabelName() {
    const select = document.getElementById('label-size');
    if (!select) return '';
    const entry = (typeof labelEntryFor === 'function') ? labelEntryFor(select.value) : null;
    return entry ? entry.name : (select.value || 'no label type');
}

/**
 * Is the configured label type one of the ones the printer reports?
 * Unknown media never counts as a disagreement - the printer has not said
 * anything to disagree with.
 * @returns {boolean}
 */
function configuredMatchesLoaded() {
    const select = document.getElementById('label-size');
    if (!select || loadedMedia.candidates.length === 0) return true;
    return loadedMedia.candidates.indexOf(select.value) !== -1;
}

// ===================== The three stored settings =====================

/**
 * Take the automatic-switching settings out of a settings document.
 *
 * Every one of them is optional and every one degrades to "not configured", so
 * a settings file written before this feature (or a server that drops the keys)
 * leaves the app in exactly its previous behaviour: never switching by itself.
 *
 * @param {?Object} settings - the settings document, or anything at all
 */
function applyMediaSettings(settings) {
    const source = (settings && typeof settings === 'object') ? settings : {};

    mediaAutoSwitch = truthy(firstDefined(source, ['media_auto_switch', 'mediaAutoSwitch']));

    const rawOwned = firstDefined(source, ['owned_media', 'ownedMedia']);
    ownedMedia = Array.isArray(rawOwned)
        ? rawOwned
            .map(candidateIdentifier)
            .filter(id => id && typeof labelEntryFor === 'function' && labelEntryFor(id))
            .filter((id, index, list) => list.indexOf(id) === index)
        : [];

    const rawMemory = firstDefined(source, ['media_memory', 'mediaMemory']);
    mediaMemory = {};
    if (rawMemory && typeof rawMemory === 'object' && !Array.isArray(rawMemory)) {
        Object.keys(rawMemory).forEach(key => {
            const value = candidateIdentifier(rawMemory[key]);
            // A remembered choice that is not one of the identifiers its medium
            // can be addressed by would switch, say, a continuous roll to a
            // die-cut label size - the one outcome this whole feature avoids.
            const group = MEDIA_VARIANT_GROUPS[key];
            if (value && group && group.indexOf(value) !== -1) mediaMemory[key] = value;
        });
    }

    // Seed the memory from the label type that is already configured. Without
    // this, the very first 62 mm roll after turning automatic mode on would
    // stall on an ambiguity the user has in fact already answered - every time
    // they have ever printed on 62 mm they answered it.
    const select = document.getElementById('label-size');
    if (select) seedMediaMemory(select.value);

    // A fresh load carries no manual override forward.
    autoSwitchSuppressedFor = null;

    renderOwnedMedia();
    syncAutoSwitchControl();
    reapplyOwnedNarrowing();
}

/**
 * The settings this module owns, as a patch for the settings document.
 * @returns {{media_auto_switch: boolean, owned_media: string[], media_memory: Object}}
 */
function mediaSettingsPatch() {
    return {
        media_auto_switch: mediaAutoSwitch,
        owned_media: ownedMedia.slice(),
        media_memory: Object.assign({}, mediaMemory)
    };
}

/**
 * Record that this label type is what was settled on for the loaded medium.
 *
 * Two conditions, both of them the point of the thing:
 *
 *   * only the three genuinely ambiguous media are remembered - every other
 *     identifier is pinned by detection alone and has nothing to remember;
 *   * only when the medium in the printer could actually be it. Choosing
 *     102x51 while a 62 mm roll is loaded is preparing a job for a roll that is
 *     not in yet; filing that against the 62 mm roll would record the wrong
 *     thing and then act on it later.
 *
 * @param {string} identifier - the label type now in use
 * @returns {boolean} whether this changed the memory
 */
function rememberMediaChoice(identifier) {
    const key = mediaMemoryKeyFor(identifier);
    if (!key || mediaMemory[key] === identifier) return false;
    if (loadedMedia.candidates.indexOf(identifier) === -1) return false;
    mediaMemory[key] = identifier;
    return true;
}

/**
 * Take the label type already in use as the answer for its own medium, unless
 * something is stored for it already.
 *
 * This is what stops a fresh install stalling on a question its user has in
 * effect already answered: every time they have printed on 62 mm, they said
 * which 62 mm roll it was. It never overwrites a stored choice, and it is the
 * one entry not gated on a detection - there is no detection yet when the
 * settings load.
 *
 * @param {string} identifier - the configured label type
 */
function seedMediaMemory(identifier) {
    const key = mediaMemoryKeyFor(identifier);
    if (key && !mediaMemory[key]) mediaMemory[key] = identifier;
}

/**
 * Is this label type on the user's list of media they own?
 * @param {string} identifier - a label type identifier
 * @returns {boolean}
 */
function ownsMedium(identifier) {
    return ownedMedia.indexOf(identifier) !== -1;
}

/**
 * Add or remove a label type from the list of media the user owns, and store
 * the list straight away - it is a statement about the room, not a draft.
 * @param {string} identifier - a label type identifier
 */
function toggleOwnedMedium(identifier) {
    if (!identifier || (typeof labelEntryFor === 'function' && !labelEntryFor(identifier))) return;

    const index = ownedMedia.indexOf(identifier);
    if (index === -1) {
        ownedMedia.push(identifier);
    } else {
        ownedMedia.splice(index, 1);
    }

    renderOwnedMedia();
    reapplyOwnedNarrowing();
    persistMediaSettings(ownedMediaSaveMessage());
}

/**
 * Re-decide the roll that is in the printer right now from the list as it
 * stands. Saying "I only own the plain 62 mm roll" has to settle the roll
 * already sitting in the machine, not just the next one.
 */
function reapplyOwnedNarrowing() {
    loadedMedia = narrowByOwnedMedia(reportedMedia);

    if (typeof setLabelPickerLoadedMedia === 'function') {
        setLabelPickerLoadedMedia({
            candidates: loadedMedia.candidates,
            reason: mediaAmbiguityReason(loadedMedia)
        });
    }
    refreshMediaUI();
    maybeAutoSwitchMedia();
}

/**
 * Turn automatic switching on or off and store it straight away.
 * @param {boolean} enabled - whether the app should follow the printer itself
 */
function setMediaAutoSwitch(enabled) {
    const wanted = !!enabled;
    if (wanted === mediaAutoSwitch) return;
    mediaAutoSwitch = wanted;
    // Turning the mode on is itself an instruction about the roll in there now,
    // so an earlier manual choice no longer holds it off.
    autoSwitchSuppressedFor = null;

    syncAutoSwitchControl();
    refreshMediaUI();
    persistMediaSettings(mediaAutoSwitch
        ? 'Automatic switching is on — the app will follow the printer.'
        : 'Automatic switching is off — the app will ask first.');

    // Turning it on with a roll already sitting there should act on that roll
    // rather than wait for the next one.
    if (mediaAutoSwitch) maybeAutoSwitchMedia();
}

// ===================== Resolving an ambiguity =====================

/**
 * Narrow a report to the media the user says they own.
 *
 * Only ever applied to a genuine ambiguity, and only when something is left: a
 * list that rules out every candidate is a list that is out of date, and
 * believing it would hide the roll that is actually in the printer.
 *
 * @param {Object} info - a parsed media report
 * @returns {Object} the same report, or a narrowed copy of it
 */
function narrowByOwnedMedia(info) {
    if (!info || info.candidates.length < 2 || ownedMedia.length === 0) return info;

    const kept = info.candidates.filter(ownsMedium);
    if (kept.length === 0 || kept.length === info.candidates.length) return info;

    return Object.assign({}, info, {
        state: kept.length === 1 ? 'identified' : 'ambiguous',
        candidates: kept,
        narrowedByOwned: true,
        // The server's resolution survives only if it is still on the table.
        resolved: kept.indexOf(info.resolved) !== -1 ? info.resolved : '',
        resolvedBy: kept.indexOf(info.resolved) !== -1 ? info.resolvedBy : '',
        resolvedReason: kept.indexOf(info.resolved) !== -1 ? info.resolvedReason : ''
    });
}

/**
 * The label type an automatic switch should move to, and why.
 *
 * Returns an empty identifier when nothing resolves the ambiguity, which is the
 * case automatic mode must not guess its way out of.
 *
 * @param {Object} info - the current, already narrowed report
 * @returns {{identifier: string, reason: string}}
 */
function autoSwitchTarget(info) {
    if (!info || info.candidates.length === 0) return { identifier: '', reason: '' };

    // The server said, in as many words, that it must not be picked.
    if (info.autoAction === 'ambiguous') return { identifier: '', reason: '' };

    if (info.candidates.length === 1) {
        return {
            identifier: info.candidates[0],
            reason: info.narrowedByOwned
                ? 'it is the only one of the detected types on your list of media you own'
                : 'the printer identified it outright'
        };
    }

    // The server's own decision, taken from the same stored settings.
    if (info.autoTo) {
        return { identifier: info.autoTo, reason: info.autoReason || info.resolvedReason ||
            'the printer’s report resolved it' };
    }
    if (info.resolved) {
        return {
            identifier: info.resolved,
            reason: info.resolvedReason || 'the printer’s report resolved it'
        };
    }

    // Nothing came back from the server: run the same recollection here.
    const remembered = rememberedChoiceFor(info.candidates);
    if (remembered) {
        return {
            identifier: remembered,
            reason: 'it is the one you used last time this roll was loaded'
        };
    }

    return { identifier: '', reason: '' };
}

/**
 * Follow the printer, if that is what the user asked for and the report leaves
 * no room for a guess.
 *
 * The switch is made by writing #label-size and firing its "change" event -
 * the same single path the Settings picker, the top bar switcher and the
 * mismatch buttons all take - so it is persisted exactly once, by the one
 * listener that already does it, and nothing goes round the source of truth.
 *
 * @returns {string} the identifier switched to, or an empty string
 */
function maybeAutoSwitchMedia() {
    if (!mediaAutoSwitch) return '';
    if (loadedMedia.candidates.length === 0 || configuredMatchesLoaded()) return '';
    if (autoSwitchSuppressedFor !== null && autoSwitchSuppressedFor === mediaKey(loadedMedia)) return '';

    // The list is open and the user is reading it: switching under them would
    // close it and move the selection. It happens when the list closes instead.
    if (typeof labelPickerIsOpen === 'function' && labelPickerIsOpen()) return '';

    const target = autoSwitchTarget(loadedMedia);
    if (!target.identifier) return '';

    const select = document.getElementById('label-size');
    if (!select || select.value === target.identifier) return '';

    pendingAutoSwitchReason = target.reason;
    if (typeof selectLabelIdentifier === 'function') {
        selectLabelIdentifier(target.identifier);
    }
    // selectLabelIdentifier declines an identifier the <select> does not carry;
    // a note left behind would then be attached to the user's next manual pick.
    if (select.value !== target.identifier) {
        pendingAutoSwitchReason = '';
        return '';
    }
    return target.identifier;
}

/**
 * Why the last switch happened, set just before #label-size is written and read
 * once by the "change" listener. Empty for every change the user made.
 */
let pendingAutoSwitchReason = '';

/**
 * The candidate set a manual choice was made against, if any.
 *
 * Choosing a label type by hand while a roll is loaded is a deliberate
 * statement - preparing a job for the next roll, or overruling a detection -
 * and automatic mode undoing it thirty seconds later would make the app
 * unusable for exactly the people who turned the mode on. So a manual choice
 * holds automatic switching off until the printer reports something else; a new
 * roll changes the key and the mode picks up again.
 */
let autoSwitchSuppressedFor = null;

/**
 * Called by the picker when it closes, so a switch held back while a list was
 * open happens the moment it is not.
 */
function mediaPickerClosed() {
    maybeAutoSwitchMedia();
}

/**
 * Take the pending reason, clearing it.
 * @returns {string}
 */
function consumeAutoSwitchReason() {
    const reason = pendingAutoSwitchReason;
    pendingAutoSwitchReason = '';
    return reason;
}

// ===================== Taking a status response =====================

/**
 * Take the printer status response and refresh everything that depends on it.
 * Called from checkPrinterStatus() in api.js, including on failure (with null),
 * so an unreachable printer clears the detection rather than freezing it.
 *
 * @param {*} data - the parsed status response, or null when the check failed
 */
function applyPrinterStatusMedia(data) {
    const before = loadedMedia;
    // Parsing reads the wire and nothing else; narrowing is this app's policy
    // about what could really be in the room, so it happens afterwards and
    // every surface below sees the same, narrowed set.
    reportedMedia = parseLoadedMedia(data);
    loadedMedia = narrowByOwnedMedia(reportedMedia);

    if (loadedMedia.candidates.length > 0) {
        lastSeenMedia = loadedMedia;
        cancelMediaRecheck();
    } else if (before.candidates.length > 0) {
        // A roll that was there a moment ago is gone: someone is swapping it.
        // Look again shortly rather than making them wait out the 30 s poll.
        scheduleMediaRecheck();
    }

    if (typeof setLabelPickerLoadedMedia === 'function') {
        setLabelPickerLoadedMedia({
            candidates: loadedMedia.candidates,
            reason: mediaAmbiguityReason(loadedMedia)
        });
    }

    refreshMediaUI();

    // Switch first, then announce: after a successful switch the roll and the
    // settings agree, and the announcement should say that rather than raise a
    // disagreement that no longer exists.
    const switched = maybeAutoSwitchMedia();
    if (switched) announcedMediaKey = mediaKey(loadedMedia);
    announceMediaChange();
}

/**
 * Repaint the pill and the mismatch warning from the current state. Safe to
 * call at any time; used by the settings load and by the label picker too.
 */
function refreshMediaUI() {
    updateMediumPill();
    updateMediaMismatch();
}

/**
 * Two bounded, one-shot re-checks after the roll disappears.
 *
 * This is not a second polling loop: it fires at most twice per swap, is
 * cancelled the moment a roll is seen, and calls the same status endpoint the
 * 30 s poll calls. Without it a roll change takes up to half a minute to show
 * up, which is exactly the moment the feature exists for.
 */
function scheduleMediaRecheck() {
    cancelMediaRecheck();
    [6000, 16000].forEach(delay => {
        mediaRecheckTimers.push(setTimeout(() => {
            if (typeof checkPrinterStatus === 'function') checkPrinterStatus();
        }, delay));
    });
}

/**
 * Drop any pending re-check.
 */
function cancelMediaRecheck() {
    mediaRecheckTimers.forEach(clearTimeout);
    mediaRecheckTimers = [];
}

/**
 * Tell the user once when the loaded roll changes to something new.
 *
 * The very first report is recorded silently: the pill and the warning already
 * state it, and a toast on every page load would be noise. After that, a roll
 * that has actually been swapped is worth a line.
 */
function announceMediaChange() {
    const key = mediaKey(loadedMedia);
    if (!key) return;

    const first = announcedMediaKey === null;
    if (key === announcedMediaKey) return;
    announcedMediaKey = key;
    if (first || typeof showNotification !== 'function') return;

    const name = mediaDisplayName(loadedMedia);
    if (configuredMatchesLoaded()) {
        showNotification('The printer now has ' + name + ' loaded, which is what the app prints for.', 'success');
    } else if (autoSwitchIsStuck()) {
        // Automatic mode is on and did not act. Saying so is the point: a mode
        // that silently does nothing is indistinguishable from one that is off.
        showNotification('The printer now has ' + name + ' loaded, and automatic switching cannot ' +
            'tell which of these rolls it is. Pick one below — the app is still set to ' +
            configuredLabelName() + '.', 'warning');
    } else {
        showNotification('The printer now has ' + name + ' loaded; the app is set to ' +
            configuredLabelName() + '.', 'warning');
    }
}

/**
 * Is automatic switching on, wanted here, and unable to decide?
 *
 * This is the one state the feature is allowed to leave the user in, so it is
 * named once and said out loud in all three places that can say it.
 * @returns {boolean}
 */
function autoSwitchIsStuck() {
    return mediaAutoSwitch &&
        loadedMedia.candidates.length > 1 &&
        !configuredMatchesLoaded() &&
        !autoSwitchTarget(loadedMedia).identifier;
}

// ===================== The top bar pill =====================

/**
 * What the pill should say and how loudly.
 *
 * Four states, and only one of them is loud:
 *   identified / ambiguous - green when the settings agree with the printer,
 *     red when they do not (the error this whole feature exists to catch);
 *   nothing loaded, cover open - neutral. A roll change passes through these
 *     for as long as it takes to open the printer, and an alarm every time
 *     someone changes paper would train people to ignore the pill;
 *   unknown - neutral, and it falls back to naming the configured label type.
 *     The pill is also the roll switcher, so hiding it would take away a
 *     control; naming the configured type with a muted dot says "this is what
 *     the app will print for, unconfirmed" without claiming the printer said so.
 *
 * @returns {{state: string, text: string, title: string}}
 */
function mediumPillView() {
    const configured = configuredLabelName();

    if (loadedMedia.candidates.length > 0) {
        const name = mediaDisplayName(loadedMedia);
        const ambiguous = loadedMedia.candidates.length > 1;
        if (configuredMatchesLoaded()) {
            return {
                state: 'ok',
                text: name,
                title: 'The printer has ' + name + ' loaded and the app prints for ' +
                    configured + '. Click to change the roll.'
            };
        }
        return {
            state: 'warn',
            text: name,
            title: 'The printer has ' + name + ' loaded; the app is set to ' + configured +
                (ambiguous ? '. Click to pick which of the loaded types it is.' : '. Click to switch.')
        };
    }

    if (loadedMedia.coverOpen) {
        return {
            state: 'idle',
            text: 'Cover open',
            title: 'The printer cover is open. The app still prints for ' + configured + '.'
        };
    }

    if (loadedMedia.state === 'none') {
        const was = lastSeenMedia ? mediaDisplayName(lastSeenMedia) : '';
        return was
            ? {
                state: 'idle',
                text: 'Changing roll…',
                title: 'No roll loaded — ' + was + ' was in it a moment ago. The app prints for ' +
                    configured + '. Click to change the label type.'
            }
            : {
                state: 'idle',
                text: 'No roll',
                title: 'The printer reports no roll loaded. The app prints for ' + configured +
                    '. Click to change the label type.'
            };
    }

    // Nothing identified. The server may still say why - an unreachable
    // printer, a backend that has no status channel, or a medium this app has
    // no label type for - which is worth passing on verbatim.
    return {
        state: 'idle',
        text: configured,
        title: (loadedMedia.reason ? loadedMedia.reason + '. ' : '') +
            'The printer did not report which roll is loaded, so this is the label type ' +
            'the app is set to print for. Click to change it.'
    };
}

/**
 * Paint the medium pill in the top bar.
 */
function updateMediumPill() {
    const pill = document.getElementById('navbar-medium');
    const text = document.getElementById('medium-indicator');
    if (!pill || !text) return;

    const view = mediumPillView();
    const auto = mediaAutoSwitch
        ? ' Automatic switching is on: the app follows the roll the printer reports.'
        : '';

    text.textContent = view.text;
    pill.setAttribute('title', view.title + auto);
    pill.setAttribute('aria-label', 'Loaded label roll: ' + view.title + auto);
    pill.classList.remove('media-ok', 'media-warn', 'media-idle');
    pill.classList.add('media-' + view.state);

    // A setting that changes things by itself has to be visible while it does,
    // and the pill is the thing it changes. A four-letter chip is all the
    // cluster can spare; on a phone the same element becomes a corner dot.
    const marker = document.getElementById('medium-auto');
    if (marker) {
        marker.hidden = !mediaAutoSwitch;
        marker.setAttribute('title', 'Automatic media switching is on');
    }
    pill.classList.toggle('is-auto', mediaAutoSwitch);
}

/**
 * Reflect the stored automatic-switching setting in its control.
 */
function syncAutoSwitchControl() {
    const select = document.getElementById('media-auto-switch');
    if (select) select.value = mediaAutoSwitch ? 'true' : 'false';
    updateMediumPill();
}

/**
 * One line about the loaded roll for the printer status dialog, as an HTML
 * fragment to append after the status text. Empty when nothing is known, so a
 * server without the feature leaves the dialog exactly as it was.
 * @returns {string}
 */
function mediaStatusLine() {
    const escape = (typeof escapeHtml === 'function') ? escapeHtml : (value => String(value));

    if (loadedMedia.candidates.length > 0) {
        const name = escape(mediaDisplayName(loadedMedia));
        return configuredMatchesLoaded()
            ? '<br><span class="media-status-line">Loaded: ' + name + '</span>'
            : '<br><span class="media-status-line is-warn">Loaded: ' + name +
              ' — the app is set to ' + escape(configuredLabelName()) + '</span>';
    }
    if (loadedMedia.coverOpen) {
        return '<br><span class="media-status-line">Cover open</span>';
    }
    if (loadedMedia.state === 'none') {
        return '<br><span class="media-status-line">No roll loaded</span>';
    }
    return '';
}

// ===================== The mismatch warning =====================

/**
 * Show, next to the label picker, that the printer has one roll in it and the
 * settings say another - and offer the way across.
 *
 * A single candidate gets a one-click switch. Several candidates get one button
 * each and no default: 62 mm paper and the 62 mm black/red roll are the same
 * geometry, so guessing would print silently and wrongly rather than failing.
 */
function updateMediaMismatch() {
    const box = document.getElementById('media-mismatch');
    const textEl = document.getElementById('media-mismatch-text');
    const reasonEl = document.getElementById('media-mismatch-reason');
    const actionsEl = document.getElementById('media-mismatch-actions');
    if (!box || !textEl || !reasonEl || !actionsEl) return;

    if (loadedMedia.candidates.length === 0 || configuredMatchesLoaded()) {
        box.classList.add('d-none');
        actionsEl.innerHTML = '';
        return;
    }

    const entries = loadedMedia.candidates
        .map(id => (typeof labelEntryFor === 'function' ? labelEntryFor(id) : null))
        .filter(entry => entry);
    const configured = configuredLabelName();

    textEl.textContent = entries.length === 1
        ? 'The printer has ' + entries[0].name + ' loaded; the settings say ' + configured + '.'
        : 'The printer has ' + mediaDisplayName(loadedMedia) + ' loaded but cannot tell which roll it is; ' +
          'the settings say ' + configured + '.';

    // Automatic mode that could not decide has to say so here too, next to the
    // buttons that are now the only way out of it.
    const stuck = autoSwitchIsStuck()
        ? 'Automatic switching is on but will not choose this one: nothing in your ' +
          'settings says which of these is loaded. Pick it once and it will be ' +
          'remembered for the next time this roll comes back.'
        : '';
    const reason = [mediaAmbiguityReason(loadedMedia), stuck].filter(part => part).join(' ');
    reasonEl.textContent = reason;
    reasonEl.hidden = !reason;

    actionsEl.innerHTML = '';
    entries.forEach(entry => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'btn-ghost btn-sm media-mismatch-action';
        button.dataset.identifier = entry.identifier;
        button.textContent = entries.length === 1
            ? 'Switch to ' + entry.name
            : entry.name;
        actionsEl.appendChild(button);
    });

    box.classList.remove('d-none');
}

// ===================== The list of media the user owns =====================

/**
 * The line that says what was just stored about the owned list.
 * @returns {string}
 */
function ownedMediaSaveMessage() {
    if (ownedMedia.length === 0) {
        return 'No media listed — every detected roll is offered.';
    }
    return ownedMedia.length === 1
        ? '1 medium listed as yours.'
        : ownedMedia.length + ' media listed as yours.';
}

/**
 * Paint the chips under the owned-media control.
 *
 * The chips are the whole reason the picker can stay a plain list: what has
 * been ticked is read off here, not by scrolling thirty rows looking for marks.
 */
function renderOwnedMedia() {
    const list = document.getElementById('owned-media-chips');
    const emptyEl = document.getElementById('owned-media-empty');
    const countEl = document.getElementById('owned-media-count');
    if (countEl) {
        countEl.textContent = ownedMedia.length > 0 ? String(ownedMedia.length) : '';
        countEl.hidden = ownedMedia.length === 0;
    }
    if (emptyEl) emptyEl.hidden = ownedMedia.length > 0;
    if (!list) return;

    list.innerHTML = '';
    ownedMedia.forEach(identifier => {
        const entry = (typeof labelEntryFor === 'function') ? labelEntryFor(identifier) : null;
        const name = entry ? entry.name : identifier;

        const item = document.createElement('li');
        item.className = 'owned-chip';

        const text = document.createElement('span');
        text.className = 'owned-chip-name';
        text.textContent = name;
        item.appendChild(text);

        const remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'owned-chip-remove';
        remove.dataset.identifier = identifier;
        remove.setAttribute('aria-label', 'Remove ' + name + ' from the media you own');
        remove.setAttribute('title', 'Remove ' + name);
        remove.innerHTML = '<i class="bi bi-x" aria-hidden="true"></i>';
        item.appendChild(remove);

        list.appendChild(item);
    });
}

// ===================== Persisting the media settings =====================

/**
 * Read the stored settings document, or null when it cannot be read.
 * @returns {Promise<?Object>}
 */
async function readSettingsDocument() {
    try {
        const response = await fetch('/api/v1/settings');
        if (!response.ok) return null;
        return await response.json();
    } catch (error) {
        console.error('Error reading settings for the label type:', error);
        return null;
    }
}

/**
 * Say whether something in the Media block is stored on the server yet.
 *
 * One line for the whole block: the label type, the automatic mode and the
 * owned list are all written the same way and all report through here, so
 * there is never a question of which of them the message is about.
 *
 * @param {string} state - 'saving' | 'saved' | 'failed'
 * @param {string} message - what to say
 */
function setMediaSaveState(state, message) {
    const el = document.getElementById('label-save-state');
    if (!el) return;

    el.classList.remove('is-saving', 'is-saved', 'is-failed');
    el.hidden = false;
    el.classList.add(state === 'saving' ? 'is-saving' : (state === 'saved' ? 'is-saved' : 'is-failed'));
    el.textContent = message;
}

/**
 * Say whether the label type is stored on the server yet.
 * @param {string} state - 'saving' | 'saved' | 'failed'
 * @param {string} name - the label type's display name
 */
function setLabelSaveState(state, name) {
    if (state === 'saving') {
        setMediaSaveState('saving', 'Saving ' + name + '…');
    } else if (state === 'saved') {
        setMediaSaveState('saved', 'Saved — the app prints for ' + name + '.');
    } else {
        setMediaSaveState('failed', 'Set to ' + name + ' for this session only; it could not be saved. ' +
            'Use Save Settings to try again.');
    }
}

/**
 * Write a partial settings document, the same shape the calibration writes use,
 * so an unsaved edit elsewhere in the Settings form is left alone.
 *
 * @param {Object} patch - the keys to change
 * @returns {Promise<boolean>} whether the server took it
 * @throws {Error} carrying the server's message when it did not
 */
async function putSettingsPatch(patch) {
    const base = await readSettingsDocument();
    const uriEl = document.getElementById('printer-uri');
    const modelEl = document.getElementById('printer-model');
    const select = document.getElementById('label-size');
    const body = Object.assign({
        printer_uri: (base && base.printer_uri) || (uriEl ? uriEl.value : ''),
        printer_model: (base && base.printer_model) || (modelEl ? modelEl.value : ''),
        label_size: (select && select.value) || (base && base.label_size) || ''
    }, patch);

    const response = await fetch('/api/v1/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
    });
    if (!response.ok) {
        let message = 'Error: ' + response.status;
        try {
            const data = await response.json();
            message = data.message || data.details || message;
        } catch (e) {
            // Non-JSON body: keep the generic message.
        }
        throw new Error(message);
    }
    return true;
}

/**
 * Store the automatic-switching settings.
 *
 * These are preferences rather than facts about the printer, so a failure is
 * reported in the block's own line and does not raise a toast: nothing the user
 * is looking at changes meaning because the write did not land.
 *
 * @param {string} message - what to say once it is stored
 * @returns {Promise<boolean>} whether it was stored
 */
async function persistMediaSettings(message) {
    setMediaSaveState('saving', 'Saving…');
    try {
        await putSettingsPatch(mediaSettingsPatch());
        setMediaSaveState('saved', message);
        return true;
    } catch (error) {
        console.error('Error saving the media settings:', error);
        setMediaSaveState('failed', 'Applied for this session only; it could not be saved: ' +
            error.message);
        return false;
    }
}

/**
 * Store the label type on the server.
 *
 * The label type is a statement about which roll is physically in the printer,
 * and every way of changing it - the top bar's switcher, the Settings picker,
 * the mismatch buttons and an automatic switch - runs through this one
 * function, so none of them can appear to stick while another quietly does not.
 *
 * The memory of which roll was chosen for an ambiguous group rides along in the
 * same write: it is set by the very act of choosing, and a second request to
 * store it could fail on its own and leave the two disagreeing.
 *
 * @param {string} identifier - the label type identifier
 * @param {string} [autoReason] - why the app switched by itself; empty when the
 *   user did it
 * @returns {Promise<boolean>} whether it was stored
 */
async function persistLabelSize(identifier, autoReason) {
    const entry = (typeof labelEntryFor === 'function') ? labelEntryFor(identifier) : null;
    const name = entry ? entry.name : identifier;
    const automatic = !!autoReason;

    rememberMediaChoice(identifier);
    setLabelSaveState('saving', name);

    try {
        await putSettingsPatch(Object.assign({ label_size: identifier }, mediaSettingsPatch()));

        if (automatic) {
            setMediaSaveState('saved', 'Switched automatically to ' + name + ' — ' + autoReason + '.');
        } else {
            setLabelSaveState('saved', name);
        }
        if (typeof showNotification === 'function') {
            showNotification(automatic
                ? 'Switched to ' + name + ' automatically, because ' + autoReason + '.'
                : 'Label type set to ' + name + ' and saved.', 'success');
        }
        return true;
    } catch (error) {
        console.error('Error saving the label type:', error);
        setLabelSaveState('failed', name);
        if (typeof showNotification === 'function') {
            showNotification((automatic
                ? 'Switched to ' + name + ' automatically'
                : 'Label type set to ' + name) +
                ' for this session, but it could not be saved: ' + error.message, 'warning');
        }
        return false;
    }
}

// ===================== Wiring =====================

/**
 * Wire up the loaded-media UI. Called from setupEventListeners() in core.js,
 * after setupLabelPicker() has taken over the native dropdown.
 */
function setupMediumSwitcher() {
    const pill = document.getElementById('navbar-medium');
    const select = document.getElementById('label-size');

    if (pill) {
        pill.addEventListener('click', () => {
            const opening = (typeof labelPickerIsOpenIn !== 'function') ||
                !labelPickerIsOpenIn('medium-switcher');
            if (typeof toggleLabelPicker === 'function') toggleLabelPicker('medium-switcher');
            // Ask the printer again as the list opens, so the candidates the
            // user is about to choose from are current rather than up to 30 s
            // old. Same call the poll makes; one extra request per opening.
            if (opening && typeof checkPrinterStatus === 'function') checkPrinterStatus();
        });
        pill.addEventListener('keydown', event => {
            if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                event.preventDefault();
                if (typeof openLabelPicker === 'function') openLabelPicker('medium-switcher');
            }
        });
    }

    if (select) {
        // One listener for every way the label type can be chosen: the Settings
        // picker, the top bar switcher, the mismatch warning and an automatic
        // switch all end up here, because they all write to #label-size and
        // fire "change". An automatic switch leaves its reason behind rather
        // than taking a path of its own.
        select.addEventListener('change', () => {
            const autoReason = consumeAutoSwitchReason();
            // A choice the user made overrules automatic mode for this roll.
            autoSwitchSuppressedFor = autoReason ? null : mediaKey(loadedMedia);
            refreshMediaUI();
            persistLabelSize(select.value, autoReason);
        });
    }

    // The mismatch warning's buttons are rebuilt on every report, so they are
    // handled by delegation.
    const actions = document.getElementById('media-mismatch-actions');
    if (actions) {
        actions.addEventListener('click', event => {
            const button = event.target.closest('.media-mismatch-action');
            if (!button || !button.dataset.identifier) return;
            if (typeof selectLabelIdentifier === 'function') {
                selectLabelIdentifier(button.dataset.identifier);
            }
        });
    }

    // Automatic switching. Off is the stored default; turning it on is a
    // deliberate act, so it is stored the moment it is made.
    const auto = document.getElementById('media-auto-switch');
    if (auto) {
        auto.addEventListener('change', () => setMediaAutoSwitch(auto.value === 'true'));
    }

    // The list of media the user owns, kept in the one picker that already
    // knows the catalogue, the product codes and the grouping.
    const add = document.getElementById('owned-media-add');
    if (add) {
        add.addEventListener('click', () => {
            if (typeof toggleLabelPicker === 'function') toggleLabelPicker('owned-media');
        });
        add.addEventListener('keydown', event => {
            if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                event.preventDefault();
                if (typeof openLabelPicker === 'function') openLabelPicker('owned-media');
            }
        });
    }

    // Chips are rebuilt whenever the list changes, so removal is delegated.
    const chips = document.getElementById('owned-media-chips');
    if (chips) {
        chips.addEventListener('click', event => {
            const button = event.target.closest('.owned-chip-remove');
            if (button && button.dataset.identifier) toggleOwnedMedium(button.dataset.identifier);
        });
    }

    renderOwnedMedia();
    syncAutoSwitchControl();
    refreshMediaUI();
}
