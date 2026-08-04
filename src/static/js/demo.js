// Brother QL Printer App - Standalone Demo Mode
//
// This script lets the static UI run on GitHub Pages (or any static host)
// without the Python backend. It is completely inert unless a demo context is
// detected, so the backend-served app is never affected.
//
// When active, it monkey-patches window.fetch and serves all /api/v1/* requests
// from an in-memory mock so the interface looks fully alive: settings populate,
// the queue is seeded with sample jobs, printing pushes new jobs and previews
// render a placeholder label. Nothing is ever sent to a real printer.

(function () {
    'use strict';

    // Activate only in a demo context: on a *.github.io host, or when the URL
    // carries a ?demo flag (handy for local testing against the static files).
    const DEMO = location.hostname.endsWith('github.io') ||
        new URLSearchParams(location.search).has('demo');
    if (!DEMO) return;

    // ===================== Demo state =====================
    //
    // Mirrors the shape of DEFAULT_SETTINGS (config/default_settings.py) so the
    // settings form populates cleanly, plus an in-memory job registry + queue
    // control state so the Queue panel is interactive.

    const state = {
        settings: {
            printer_uri: 'tcp://printer.local',
            printer_model: 'QL-820NWB',
            label_size: '62',
            font_size: 50,
            alignment: 'left',
            rotate: 0,
            threshold: 70.0,
            dither: false,
            compress: false,
            red: false,
            copies: 1,
            cut_mode: 'each',
            dpi_600: false,
            hq: true,
            keep_alive_enabled: true,
            keep_alive_interval: 60,
            // A timed window rather than "forever", because the whole relay
            // timing chain hangs off one: the turn-off moment is measured from
            // the end of it, and with no window there is nothing for the
            // Settings panel to draw. 4 h with a 10 min device timer is the
            // configuration the documentation works through, so the demo shows
            // the chain everybody has already read about.
            keep_alive_mode: 'timed',
            keep_alive_duration_seconds: 14400,
            ipp_port: 631,
            // Relay power control, switched on. The demo exists to show what
            // the app does, and a feature that is off shows one master switch
            // and nothing else. The turn-off half is armed too: it is the half
            // that produces a scheduled moment, and the countdown to it is the
            // one part of this chain that visibly moves.
            //
            // The URL is a plausible LAN address for a Shelly-style relay. It
            // is never called: the fetch patch below answers the send endpoint
            // itself, so nothing leaves the browser.
            relay_webhook_enabled: true,
            relay_webhook_turn_on_url: 'http://192.168.1.42/relay/0?turn=on',
            relay_webhook_turn_off_url: '',
            relay_webhook_turn_off_enabled: true,
            relay_webhook_turn_off_delay_minutes: 5,
            printer_auto_power_off_minutes: 10,
            printers: [
                {
                    id: 'default',
                    name: 'Demo Printer',
                    printer_uri: 'tcp://printer.local',
                    printer_model: 'QL-820NWB',
                    label_size: '62'
                }
            ]
        },
        keepAlive: {
            enabled: true,
            interval: 60,
            running: true
        },
        jobs: [],
        // `activityJobId` is the mock's version of the queue worker's
        // "job in hand": the one job an activity may be attributed to, so
        // /jobs and /jobs/queue can never disagree about which job is busy.
        queue: { paused: false, queued: 0, printing: 0, activityJobId: null },
        relay: {
            // The moment the timing chain is measured from. The seeded jobs are
            // written as if they had just run, so the chain starts from now and
            // every print pushes it along again.
            lastPrintAt: Date.now(),
            // What the simulated relay was last told to do, and when. Written
            // by the cold start below, which is the only thing in the demo that
            // would send a webhook if there were anything to send it to.
            lastAction: null,
            lastActionAt: null,
            // A hand-fired webhook records itself here rather than under
            // last_action: in the demo nothing is delivered, and a "last
            // webhook: turn_on" line would say it was.
            lastError: null,
            lastErrorAt: null,
            // Whether the simulated printer has mains power. False at load, so
            // the first print tells the cold-start story; see coldStartPhases.
            printerPowered: false
        }
    };

    /**
     * Generate a uuid-hex-like identifier (32 hex chars) for demo jobs.
     */
    function hexId() {
        let s = '';
        for (let i = 0; i < 32; i++) {
            s += Math.floor(Math.random() * 16).toString(16);
        }
        return s;
    }

    /**
     * ISO timestamp a given number of seconds in the past.
     */
    function isoAgo(secondsAgo) {
        return new Date(Date.now() - secondsAgo * 1000).toISOString();
    }

    // Seed a few sample jobs of varied type/status so the Queue looks populated.
    state.jobs = [
        {
            // Easter egg: "Open" / "Reprint" on this one never gonna let you down.
            id: 'rickroll',
            type: 'label',
            status: 'done',
            label: 'Never Gonna Give You Up',
            created_at: isoAgo(3),
            started_at: isoAgo(2),
            finished_at: isoAgo(1),
            error: null,
            params: {
                type: 'label',
                text: 'Never Gonna Give You Up',
                data: 'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
                settings: { font_size: 40, alignment: 'center', qr_position: 'right', error_correction: 'M' }
            },
            can_reprint: true
        },
        {
            id: hexId(),
            type: 'label',
            status: 'queued',
            label: 'Shelf A-12 + QR',
            created_at: isoAgo(8),
            started_at: null,
            finished_at: null,
            error: null,
            params: {
                type: 'label',
                text: 'Shelf A-12',
                data: 'https://example.com/shelf/A-12',
                settings: { font_size: 30, alignment: 'left', qr_position: 'right', error_correction: 'M' }
            },
            can_reprint: true
        },
        {
            id: hexId(),
            type: 'image',
            status: 'failed',
            label: 'logo.png',
            created_at: isoAgo(140),
            started_at: isoAgo(138),
            finished_at: isoAgo(136),
            error: 'Printer offline (demo): connection refused',
            params: { type: 'image', filename: 'logo.png', settings: { image_mode: 'bw-dither' } },
            can_reprint: true
        },
        {
            id: hexId(),
            type: 'qrcode',
            status: 'done',
            label: 'https://github.com/Dodoooh/brother_ql_app',
            created_at: isoAgo(320),
            started_at: isoAgo(318),
            finished_at: isoAgo(315),
            error: null,
            params: {
                type: 'qrcode',
                data: 'https://github.com/Dodoooh/brother_ql_app',
                settings: { size: 400, error_correction: 'M' }
            },
            can_reprint: true
        },
        {
            id: hexId(),
            type: 'text',
            status: 'done',
            label: 'Hello World',
            created_at: isoAgo(600),
            started_at: isoAgo(598),
            finished_at: isoAgo(596),
            error: null,
            params: {
                type: 'text',
                text: 'Hello World',
                settings: { font_size: 50, alignment: 'left' }
            },
            can_reprint: true
        }
    ];

    // Every job record carries the activity triplet, exactly as the server's
    // does: null all round for a job nothing in particular is happening to,
    // which is every seeded one. A job that was merely waiting its turn has no
    // activity, and a finished one never keeps the activity it had.
    state.jobs.forEach(job => {
        job.activity = null;
        job.activity_message = null;
        job.activity_at = null;
    });

    // A tiny light-gray label with the word "DEMO", encoded as a PNG data URL.
    // Used as the placeholder for every preview endpoint.
    const DEMO_LABEL_PNG =
        'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAUAAAACWCAYAAACAdb13AAAB+0lEQVR4nO3' +
        'BMQEAAADCoPVPbQwfoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
        'wL8B8c8AAa3l3+kAAAAASUVORK5CYII=';

    // ===================== Mock helpers =====================

    /**
     * Resolve after a small, realistic delay so the UI's loading states show.
     */
    function delay() {
        return new Promise(resolve => setTimeout(resolve, 150 + Math.random() * 150));
    }

    /**
     * Build a JSON Response with the given body and status.
     */
    function jsonResponse(body, status) {
        return new Response(JSON.stringify(body), {
            status: status || 200,
            headers: { 'Content-Type': 'application/json' }
        });
    }

    /**
     * Draw a stand-in alignment target as a PNG data URL: a crosshair through
     * the centre, concentric rings and edge ticks, which is what the real
     * server renders for a calibration test print. Drawn on a canvas rather
     * than shipped as a blob so it can follow the medium's aspect ratio.
     *
     * @param {string} labelSize - label type identifier, e.g. "d24" or "62x29"
     * @returns {string} a data:image/png URL
     */
    function calibrationTargetPng(labelSize) {
        // Square for round media, otherwise the die-cut's own ratio, otherwise
        // a strip of continuous tape.
        const round = /^d\d+$/.test(String(labelSize || ''));
        const rect = /^(\d+)x(\d+)$/.exec(String(labelSize || ''));
        const width = 420;
        let height = 260;
        if (round) height = width;
        else if (rect) height = Math.round(width * (parseInt(rect[1], 10) / parseInt(rect[2], 10)));

        const canvas = document.createElement('canvas');
        canvas.width = width;
        canvas.height = height;
        const ctx = canvas.getContext('2d');
        // No 2D context (very old or headless environment): fall back to the
        // generic placeholder rather than failing the request.
        if (!ctx) return DEMO_LABEL_PNG;

        ctx.fillStyle = '#ffffff';
        ctx.fillRect(0, 0, width, height);
        ctx.strokeStyle = '#000000';
        ctx.fillStyle = '#000000';
        ctx.lineWidth = 2;

        const cx = width / 2;
        const cy = height / 2;

        // Crosshair.
        ctx.beginPath();
        ctx.moveTo(cx, 8); ctx.lineTo(cx, height - 8);
        ctx.moveTo(8, cy); ctx.lineTo(width - 8, cy);
        ctx.stroke();

        // Concentric rings, ~5 mm apart at this scale.
        for (let r = 30; r < Math.min(width, height) / 2 - 6; r += 30) {
            ctx.beginPath();
            ctx.arc(cx, cy, r, 0, Math.PI * 2);
            ctx.stroke();
        }

        // Edge ticks: the marks you line up with the label edge.
        ctx.lineWidth = 3;
        [[cx, 0, cx, 16], [cx, height, cx, height - 16],
         [0, cy, 16, cy], [width, cy, width - 16, cy]].forEach(t => {
            ctx.beginPath();
            ctx.moveTo(t[0], t[1]); ctx.lineTo(t[2], t[3]);
            ctx.stroke();
        });

        ctx.font = '600 13px monospace';
        ctx.textAlign = 'center';
        ctx.fillText(String(labelSize || ''), cx, cy - 10);

        return canvas.toDataURL('image/png');
    }

    /**
     * Read a short, human-friendly label from a print request's body (JSON or
     * FormData), falling back to the job type.
     */
    function labelFromRequest(type, body) {
        try {
            if (body && typeof body === 'object') {
                if (body.text) {
                    return typeof body.text === 'string' ? body.text : (body.text.content || type);
                }
                if (body.qr && body.qr.data) return body.qr.data;
            }
        } catch (e) { /* ignore */ }
        return type;
    }

    // ===================== What a job is doing =====================
    //
    // The interesting half of a print in the real app happens before anything
    // is printed. A job that arrives at a printer whose mains supply is off
    // waits in the queue -- as "queued", which is what it is -- while the relay
    // is told to switch the printer on, the device boots, and the app decides
    // it is ready. Each of those phases is named by an `activity` token
    // (src/utils/job_activity.py), and the UI draws them as the rail in the
    // Queue panel and the pill in the header.
    //
    // Without the fields, the demo showed neither: a mocked print went from
    // queued to printing in under a second and the feature the queue was
    // rebuilt around was invisible on the only build most people ever see.

    // The fallback wording, mirroring ACTIVITY_MESSAGES. Used exactly where the
    // server uses it: a phase that reports a token without a sentence of its
    // own. Deliberately free of numbers, because a duration belongs to whoever
    // owns it.
    // The states a job never comes back from, and therefore never carries an
    // activity in: nothing is happening to a job that is done, failed or
    // cancelled.
    const FINISHED_STATES = ['done', 'failed', 'cancelled'];

    const ACTIVITY_MESSAGES = {
        switching_on: 'Switching the printer on at the relay.',
        waiting_for_printer: 'Waiting for the printer to come up.',
        printer_settling: 'The printer is answering; letting it settle before printing.',
        printing: 'Printing.',
        retrying: 'The print did not go through; trying again.'
    };

    /**
     * Write an activity onto a job, or clear it.
     *
     * The three fields move together, the way they do on the server: clearing
     * drops the message and the timestamp with it, so a finished job never
     * keeps the phase it was last in.
     *
     * @param {Object} job - the demo job record
     * @param {?string} activity - a token, or null to clear
     * @param {?string} [message] - the phase's own sentence; the fallback is
     *   used when a token arrives without one
     */
    function setJobActivity(job, activity, message) {
        job.activity = activity || null;
        job.activity_message = activity
            ? (message || ACTIVITY_MESSAGES[activity] || null)
            : null;
        job.activity_at = activity ? new Date().toISOString() : null;
    }

    // The cold start, in compressed seconds.
    //
    // This is the sequence the real app runs when a job arrives at a printer
    // that is switched off at the wall, with one failed print attempt in it so
    // the rail also shows `retrying` -- which is the one phase a healthy run
    // never reaches, and therefore the one nobody would otherwise see.
    //
    // The real thing takes about 45 s to a minute: 20 s of not looking at the
    // printer at all, up to 120 s of probing, a 5 s grace, then attempts 20 s
    // apart. Nobody watches a web demo for a minute, so every wait is scaled
    // down to roughly a tenth and the sentences quote the compressed numbers
    // rather than the real ones. A demo whose text says 20 s while the bar
    // moves on in 3 is a demo that has to be explained.
    //
    // The wording is otherwise the server's own, phrase for phrase
    // (relay_service._wait_until_ready, _first_pause_message and
    // queue_service._hold_before_attempt), so what is read here is what is read
    // in front of a real printer.
    const COLD_START_PHASES = [
        {
            activity: 'switching_on', status: 'queued', seconds: 2.0,
            message: 'Switching the printer on at the relay.',
            // The webhook the real gate sends at this point. In the demo it
            // moves the simulated mains state and the "last webhook" line in
            // Settings, and nothing else: no request is made.
            onEnter: function () {
                state.relay.lastAction = 'turn_on';
                state.relay.lastActionAt = new Date().toISOString();
                state.relay.printerPowered = true;
            }
        },
        {
            activity: 'waiting_for_printer', status: 'queued', seconds: 3.0,
            message: 'Switched on at the relay. Leaving the printer alone for 3s ' +
                'while it boots.'
        },
        {
            activity: 'waiting_for_printer', status: 'queued', seconds: 2.5,
            message: 'Waiting for the printer to come up.'
        },
        {
            activity: 'printer_settling', status: 'queued', seconds: 3.0,
            message: 'The printer reports itself ready. Giving it 3s before printing.'
        },
        {
            activity: 'printing', status: 'printing', seconds: 2.0,
            message: 'Printing (attempt 1 of 3).'
        },
        {
            // The built-in failure. A printer that has just been switched on can
            // refuse the first raster it is handed -- its IPP port comes and
            // goes during the boot -- which is why the real app tries three
            // times before the failure is the job's. Note the status: a job
            // between attempts is back to `queued`, not `printing`. It is not on
            // the wire, and it can still be cancelled.
            activity: 'retrying', status: 'queued', seconds: 3.0,
            message: 'Attempt 1 of 3 did not go through (the printer refused the ' +
                'raster). Trying again in 3s.'
        },
        {
            activity: 'printing', status: 'printing', seconds: 2.0,
            message: 'Printing (attempt 2 of 3).'
        }
    ];

    // A print at a printer that is already up: a moment in the queue with no
    // activity at all, then the wire. The real app does exactly this -- the
    // gate returns immediately for a printer that answers, with no webhook, no
    // wait and no retries -- and the absence of a rail is part of what the
    // demo should show, because it is what almost every print looks like.
    const WARM_PHASES = [
        { activity: null, status: 'queued', seconds: 0.7, message: null },
        { activity: 'printing', status: 'printing', seconds: 1.1, message: null }
    ];

    /**
     * Move a job through a list of phases, one timer at a time.
     *
     * Everything the two endpoints report is read from the job record itself,
     * so `GET /jobs` and `GET /jobs/queue` cannot drift apart: the queue status
     * names the job it is attributing an activity to and then reads that job.
     *
     * @param {Object} job - the demo job record
     * @param {Array<Object>} phases - the phases to walk
     * @param {number} index - which phase to enter now
     */
    function runJobPhases(job, phases, index) {
        // The job may have been cancelled or deleted while a phase was running,
        // which is a thing a user can do to every phase here except the two
        // that are on the wire. Nothing more is owed to it then.
        if (state.jobs.indexOf(job) === -1 || job.status === 'cancelled') {
            if (state.queue.activityJobId === job.id) state.queue.activityJobId = null;
            recountQueue();
            return;
        }

        const phase = phases[index];
        if (!phase) {
            job.status = 'done';
            job.finished_at = new Date().toISOString();
            setJobActivity(job, null);
            state.queue.activityJobId = null;
            // Something printed, so the timing chain restarts from here: the
            // keep-alive window, the printer's own timer and the scheduled
            // turn-off are all measured from the last print.
            state.relay.lastPrintAt = Date.now();
            recountQueue();
            return;
        }

        job.status = phase.status;
        if (phase.status === 'printing' && !job.started_at) {
            // "started" is when the printing began, not when the attempt that
            // happened to work did, so a retry does not move it.
            job.started_at = new Date().toISOString();
        }
        setJobActivity(job, phase.activity, phase.message);
        state.queue.activityJobId = phase.activity ? job.id : null;
        if (typeof phase.onEnter === 'function') phase.onEnter();
        recountQueue();

        setTimeout(() => runJobPhases(job, phases, index + 1), phase.seconds * 1000);
    }

    /**
     * Which sequence a print takes: the cold start, or straight to the wire.
     *
     * Only the first print of a session takes the cold start, and that is the
     * honest answer rather than a shortcut. The gate runs when the printer does
     * not answer; once it has been switched on it stays on -- the keep-alive
     * heartbeat is what holds it there -- so a second job a minute later really
     * does go straight to printing. Replaying the boot for every print would
     * show a printer being switched on that is already on, and would put 17 s
     * in front of every action in a demo somebody is clicking through.
     *
     * The simulated mains state is what decides it, so the story can be watched
     * again: firing turn_off by hand from Settings puts the printer back to
     * sleep, and the next print boots it again. Switching relay power control
     * off in the demo's own settings takes the whole sequence away, which is
     * also what that switch does in the real app.
     *
     * @returns {Array<Object>} the phases for this job
     */
    function phasesForPrint() {
        if (state.settings.relay_webhook_enabled && !state.relay.printerPowered) {
            return COLD_START_PHASES;
        }
        return WARM_PHASES;
    }

    /**
     * Queue a new demo job for a print request and walk it through the phases a
     * real job goes through, so the UI sees it move on its next poll.
     *
     * @param {string} type - the queue type, as shown in the job row
     * @param {Object} body - the parsed request body
     * @param {string} [paramsType] - the `params.type` the UI opens the job by,
     *   when it differs from the queue type (Text+Image). Defaults to `type`.
     */
    function queuePrintJob(type, body, paramsType) {
        const id = hexId();
        const job = {
            id: id,
            type: type,
            status: 'queued',
            label: String(labelFromRequest(type, body)).slice(0, 80),
            created_at: new Date().toISOString(),
            started_at: null,
            finished_at: null,
            error: null,
            params: Object.assign({ type: paramsType || type },
                                  body && typeof body === 'object' ? body : {}),
            can_reprint: true,
            activity: null,
            activity_message: null,
            activity_at: null
        };
        state.jobs.unshift(job);
        recountQueue();

        const phases = phasesForPrint();
        // A short beat before the first phase, so the job is visibly queued
        // before anything happens to it -- exactly as a real one is while the
        // worker picks it up.
        setTimeout(() => runJobPhases(job, phases, 0), 400);

        return id;
    }

    /**
     * Best-effort parse of a request's body into a plain object (handles JSON
     * strings and FormData with a `settings` JSON part).
     */
    async function parseBody(init, request) {
        // Prefer the Request object when fetch was called as fetch(Request).
        const src = init && 'body' in init ? init.body : (request ? request : null);
        if (!src) return {};
        try {
            if (typeof src === 'string') return JSON.parse(src);
            if (src instanceof FormData) {
                const obj = {};
                src.forEach((value, key) => {
                    if (key === 'settings' && typeof value === 'string') {
                        try { obj.settings = JSON.parse(value); } catch (e) { obj.settings = value; }
                    } else {
                        obj[key] = value;
                    }
                });
                return obj;
            }
            if (src instanceof Request) {
                const clone = src.clone();
                const text = await clone.text();
                try { return JSON.parse(text); } catch (e) { return {}; }
            }
        } catch (e) { /* ignore */ }
        return {};
    }

    // ===================== Router =====================

    /**
     * Dispatch a mocked /api/v1 request to the right handler. Returns a Response.
     * @param {string} method - upper-case HTTP method
     * @param {string} path - request pathname (without query string)
     * @param {Object} body - parsed request body
     */
    function route(method, path, body) {
        // Strip the API prefix to simplify matching.
        const p = path.replace(/^.*\/api\/v1/, '');

        // ----- Settings -----
        if (p === '/settings' && method === 'GET') {
            return jsonResponse(state.settings);
        }
        if (p === '/settings' && method === 'PUT') {
            Object.assign(state.settings, body || {});
            return jsonResponse(state.settings);
        }

        // ----- Printer status / keep-alive -----
        if (p === '/printers/status' && method === 'POST') {
            // The demo printer has the 62 mm continuous roll in it, which is
            // one of the three media the printer cannot pin down on its own:
            // plain paper and the black/red roll share a geometry.
            return jsonResponse({
                available: true,
                reachable: true,
                state: 'ready',
                blocking_reasons: [],
                status: 'Ready (demo printer)',
                media: {
                    width_mm: 62,
                    length_mm: null,
                    media_type: 'continuous',
                    is_round: false,
                    detected: true,
                    detection: 'ok',
                    candidates: ['62', '62red'],
                    ambiguous: true,
                    reason: 'The printer reports 62 mm continuous media without a colour.',
                    label_size: state.settings.label_size,
                    matches_label_size: ['62', '62red'].indexOf(state.settings.label_size) !== -1
                }
            });
        }
        if (p === '/printers/keep-alive' && method === 'GET') {
            return jsonResponse({
                enabled: state.keepAlive.enabled,
                interval: state.keepAlive.interval,
                running: state.keepAlive.running
            });
        }
        if (p === '/printers/keep-alive' && method === 'PUT') {
            if (body && typeof body.enabled === 'boolean') state.keepAlive.enabled = body.enabled;
            if (body && Number.isFinite(body.interval)) state.keepAlive.interval = body.interval;
            state.keepAlive.running = state.keepAlive.enabled;
            return jsonResponse({
                enabled: state.keepAlive.enabled,
                interval: state.keepAlive.interval,
                running: state.keepAlive.running
            });
        }
        if (p === '/printers' && method === 'GET') {
            return jsonResponse({ printers: state.settings.printers });
        }

        // ----- Relay power control -----
        if (p === '/printers/relay-power' && method === 'GET') {
            return jsonResponse(relayPowerStatus());
        }
        if (p === '/printers/relay-power/send' && method === 'POST') {
            return relayPowerSend(body && body.action);
        }

        // ----- Jobs: list + queue state -----
        if (p === '/jobs' && method === 'GET') {
            // Newest first (jobs are unshifted, but sort defensively by created_at).
            const jobs = state.jobs.slice().sort(
                (a, b) => new Date(b.created_at) - new Date(a.created_at)
            );
            return jsonResponse({ jobs: jobs });
        }
        if (p === '/jobs/queue' && method === 'GET') {
            return jsonResponse(queueStatus());
        }

        // ----- Jobs: queue control -----
        if (p === '/jobs/pause' && method === 'POST') {
            state.queue.paused = true;
            return jsonResponse(queueStatus());
        }
        if (p === '/jobs/resume' && method === 'POST') {
            state.queue.paused = false;
            return jsonResponse(queueStatus());
        }
        if (p === '/jobs/stop' && method === 'POST') {
            const cancelled = cancelQueued();
            state.queue.paused = true;
            const status = queueStatus();
            status.cancelled = cancelled;
            return jsonResponse(status);
        }
        if (p === '/jobs/clear' && method === 'POST') {
            const before = state.jobs.length;
            state.jobs = state.jobs.filter(
                j => j.status !== 'done' && j.status !== 'failed' && j.status !== 'cancelled'
            );
            return jsonResponse({ cleared: before - state.jobs.length });
        }
        if (p === '/jobs/clear-all' && method === 'POST') {
            cancelQueued();
            const before = state.jobs.length;
            // Keep a job that is currently printing, drop the rest.
            state.jobs = state.jobs.filter(j => j.status === 'printing');
            recountQueue();
            return jsonResponse({ cleared: before - state.jobs.length });
        }

        // ----- Jobs: per-id actions -----
        const idMatch = p.match(/^\/jobs\/([^/]+)(\/(cancel|delete|reprint|file))?$/);
        if (idMatch) {
            const jobId = decodeURIComponent(idMatch[1]);
            const action = idMatch[3];
            const job = state.jobs.find(j => j.id === jobId);

            if (action === 'cancel' && method === 'POST') {
                if (job && job.status === 'queued') {
                    job.status = 'cancelled';
                    job.finished_at = new Date().toISOString();
                    // A cancelled job is not waiting for a printer any more,
                    // whatever phase it was in when the button was pressed.
                    setJobActivity(job, null);
                    if (state.queue.activityJobId === job.id) {
                        state.queue.activityJobId = null;
                    }
                    recountQueue();
                    return jsonResponse({ cancelled: true });
                }
                return jsonResponse({ cancelled: false });
            }
            if (action === 'delete' && method === 'POST') {
                if (job && job.status !== 'printing') {
                    state.jobs = state.jobs.filter(j => j.id !== jobId);
                    recountQueue();
                    return jsonResponse({ removed: true });
                }
                return jsonResponse({ removed: false });
            }
            if (action === 'reprint' && method === 'POST') {
                if (!job) return jsonResponse({ message: 'Job not found' }, 404);
                const newId = queuePrintJob(job.type, job.params || {});
                return jsonResponse({ job_id: newId });
            }
            if (action === 'file' && method === 'GET') {
                // No persisted files in demo: report expired/unavailable.
                return jsonResponse({ message: 'File not available in demo' }, 404);
            }
            if (!action && method === 'GET') {
                if (!job) return jsonResponse({ message: 'Job not found' }, 404);
                return jsonResponse(job);
            }
        }

        // ----- Print endpoints -----
        // Two names per endpoint, because the real server uses two: the queue
        // type it files the job under, and the `params.type` the UI reads to
        // decide which compose form can open it again. They differ for
        // Text+Image, which is queued as a "label" but carries the params type
        // "text-image" (see text_image_controller.py). This mock used to say
        // "textimage" for both, so a demo job took a code path no real job ever
        // takes -- and the one type whose Open button was broken was the one
        // the demo could not reproduce.
        const printMap = {
            '/text/print': ['text', 'text'],
            '/qrcode/print': ['qrcode', 'qrcode'],
            '/label/text-qrcode': ['label', 'label'],
            '/image/print': ['image', 'image'],
            '/pdf/print': ['pdf', 'pdf'],
            '/label/text-image': ['label', 'text-image']
        };
        if (printMap[p] && method === 'POST') {
            const jobId = queuePrintJob(printMap[p][0], body, printMap[p][1]);
            return jsonResponse({
                success: true,
                job_id: jobId,
                message: 'Print job queued (demo)'
            });
        }

        // ----- Preview endpoints -----
        // In demo there is no server renderer, so we return no image. The app's
        // fast client-side preview (HTML/JS/CSS in preview.js) then stays visible
        // — which is exactly what we want for the demo. Locally with the Python
        // backend the real, more accurate server preview takes over instead.
        if ((p === '/text/preview' || p === '/qrcode/preview' ||
             p === '/label/preview' || p === '/image/preview') && method === 'POST') {
            return jsonResponse({});
        }
        if (p === '/pdf/preview' && method === 'POST') {
            return jsonResponse({
                total_pages: 1,
                truncated: false,
                rendered_pages: [1],
                previews: [{ page: 1, image: DEMO_LABEL_PNG }]
            });
        }

        // ----- Print alignment calibration -----
        if (p === '/calibration/preview' && method === 'POST') {
            return jsonResponse({ image: calibrationTargetPng(body && body.label_size) });
        }
        if (p === '/calibration/test-print' && method === 'POST') {
            const labelSize = (body && body.label_size) || state.settings.label_size;
            if (body && (body.dry_run === true || body.dry_run === 'true')) {
                return jsonResponse({
                    ok: true,
                    dry_run: true,
                    printer_reachable: true,
                    would_print: { label_size: labelSize, copies: 1, width_px: 696, height_px: 696 }
                });
            }
            const jobId = queuePrintJob('calibration', body || {});
            return jsonResponse({
                success: true,
                job_id: jobId,
                message: 'Calibration target queued (demo)'
            });
        }

        // ----- Share hand-off: no-op (nothing shared in demo) -----
        if (p.indexOf('/share/') === 0) {
            return jsonResponse({ message: 'Not found' }, 404);
        }

        // ----- Fallback: never throw, return a sensible empty payload -----
        if (method === 'GET') return jsonResponse({});
        return jsonResponse({});
    }

    /**
     * Current queue control status snapshot.
     *
     * It carries the current job's activity as well as the counts, because the
     * counts alone cannot tell a queue that is idle from one that is holding a
     * job while a printer boots: both report one queued job and nothing
     * printing. The activity is read off the job the mock has in hand rather
     * than kept as a second copy, so this endpoint and `GET /jobs` are two
     * views of one record and cannot contradict each other.
     */
    function queueStatus() {
        const held = state.queue.activityJobId
            ? state.jobs.find(j => j.id === state.queue.activityJobId)
            : null;
        // A job the mock still has in hand but which has already finished (it
        // was cancelled mid-phase, most often) reports no activity, whatever is
        // left on it: the counts below no longer count it, and an activity for
        // it would make the two halves of this answer contradict each other.
        const job = (held && FINISHED_STATES.indexOf(held.status) === -1) ? held : null;
        const activity = (job && job.activity) || null;
        return {
            paused: state.queue.paused,
            queued: state.jobs.filter(j => j.status === 'queued').length,
            printing: state.jobs.filter(j => j.status === 'printing').length,
            activity: activity,
            activity_message: activity ? job.activity_message : null,
            activity_at: activity ? job.activity_at : null,
            activity_job_id: activity ? job.id : null
        };
    }

    // ===================== Relay power control =====================
    //
    // The status endpoint hands the UI a timing chain that is already worked
    // out: one place decides what the timing is, and the UI only formats it.
    // This mock therefore has to do the deriving too, or the Settings panel
    // draws nothing -- which is what it did while this endpoint fell into the
    // catch-all and answered {}.
    //
    // The arithmetic below is relay_service's, in the same order:
    //
    //     effective keep-alive = window - the printer's own interval
    //                            (only while relay power control is on)
    //     printer powers off   = effective + the printer's own interval
    //     turn_off is sent     = window + the safety margin
    //
    // all measured from the last print, which the demo moves forward every time
    // a job finishes. Every moment is reported twice, absolutely and as seconds
    // remaining, because that is the contract the countdown is driven from: the
    // UI corrects for clock skew once and then ticks locally.

    // The safety warning, verbatim from AUTO_POWER_OFF_MISMATCH_WARNING. Copied
    // rather than paraphrased for the reason the UI shows it verbatim: two
    // wordings of a warning about cutting power to a running printer is one too
    // many.
    const RELAY_WARNING =
        "This app cannot read or change the printer's built-in auto-power-off " +
        'time. The value configured here is a statement about the device rather ' +
        'than a setting on it, and nothing verifies that the two agree. Set it to ' +
        "exactly what the printer's own menu shows. If the interval on the device " +
        'is longer than the value configured here, the relay will cut mains power ' +
        'while the printer is still running, which can interrupt a print mid-feed ' +
        'and can damage the printer.';

    /**
     * Seconds from now until a moment, clamped at zero, or null when there is
     * no moment.
     * @param {?number} moment - unix timestamp in seconds
     * @param {number} now - unix timestamp in seconds
     * @returns {?number}
     */
    function relayUntil(moment, now) {
        return moment === null ? null : Math.max(0, moment - now);
    }

    /**
     * ISO-8601 UTC for a unix timestamp, or null.
     * @param {?number} moment - unix timestamp in seconds
     * @returns {?string}
     */
    function relayIso(moment) {
        return moment === null ? null : new Date(moment * 1000).toISOString();
    }

    /**
     * The relay power status, derived from the demo settings the same way the
     * service derives it from the real ones.
     */
    function relayPowerStatus() {
        const s = state.settings;
        const now = Date.now() / 1000;

        const enabled = !!s.relay_webhook_enabled;
        const turnOffEnabled = !!s.relay_webhook_turn_off_enabled;
        const hardware = Math.max(0, s.printer_auto_power_off_minutes || 0) * 60;
        const delay = Math.max(0, s.relay_webhook_turn_off_delay_minutes || 0) * 60;

        // A window exists only while keep-alive is on, timed, and non-zero.
        // Nothing is scheduled without one, because the turn-off moment is
        // measured from the end of a window the app is holding open.
        const timed = s.keep_alive_mode === 'timed' &&
            (s.keep_alive_duration_seconds || 0) > 0;
        const window_ = (s.keep_alive_enabled && timed) ? s.keep_alive_duration_seconds : null;

        const offsetApplied = enabled && timed;
        const effective = window_ === null ? null
            : (enabled ? Math.max(0, window_ - hardware) : window_);
        const powerOffSeconds = effective === null ? null : effective + hardware;

        let originAt = state.relay.lastPrintAt / 1000;
        let originSource = 'print';
        let lastPrintAt = originAt;

        // The turn-off is armed by a print, so in the demo it is armed exactly
        // when there has been one -- and only while both halves are switched on.
        let scheduled = (enabled && turnOffEnabled && window_ !== null)
            ? originAt + window_ + delay
            : null;

        // A window that has already run out is re-based to now and says so:
        // "idle" means the moments below are what a print landing now would
        // start, not what is scheduled. Only reachable in a demo left open for
        // hours, which is precisely when a dead chain would look broken.
        if (window_ !== null) {
            const moments = [originAt + effective, originAt + powerOffSeconds];
            if (scheduled !== null) moments.push(scheduled);
            if (Math.max.apply(null, moments) <= now) {
                originAt = now;
                originSource = 'idle';
                scheduled = null;
            }
        }

        const keepAliveEndsAt = (window_ === null || effective === null)
            ? null : originAt + effective;
        const printerPowerOffAt = (window_ === null || powerOffSeconds === null)
            ? null : originAt + powerOffSeconds;

        // Which step the chain is waiting on. Decided here rather than left to
        // the client, exactly as the service decides it: the question is which
        // of these steps exists at all, and that is settings logic.
        let nextStep = null;
        let nextStepAt = null;
        [['keep_alive_end', keepAliveEndsAt],
         ['printer_power_off', printerPowerOffAt],
         ['turn_off', scheduled]].forEach(entry => {
            const moment = entry[1];
            if (moment === null || moment <= now) return;
            if (nextStepAt === null || moment < nextStepAt) {
                nextStep = entry[0];
                nextStepAt = moment;
            }
        });

        return {
            enabled: enabled,
            turn_off_enabled: turnOffEnabled,
            turn_on_url_configured: !!String(s.relay_webhook_turn_on_url || '').trim(),
            turn_off_url_configured: !!String(
                s.relay_webhook_turn_off_url || s.relay_webhook_turn_on_url || '').trim(),
            // No environment to read a credential from on a static host, so the
            // honest answer is "none is sent".
            authorization_configured: false,
            printer_auto_power_off_minutes: Math.round(hardware / 60),
            configured_window_seconds: window_,
            effective_keep_alive_seconds: effective,
            hardware_offset_applied: offsetApplied,
            printer_power_off_seconds: powerOffSeconds,
            turn_off_delay_seconds: delay,

            server_time: now,
            origin_at: originAt,
            origin_at_iso: relayIso(originAt),
            origin_source: originSource,
            seconds_since_origin: Math.max(0, now - originAt),
            last_print_at: lastPrintAt,
            last_print_at_iso: relayIso(lastPrintAt),
            seconds_since_last_print: Math.max(0, now - lastPrintAt),
            keep_alive_ends_at: keepAliveEndsAt,
            keep_alive_ends_at_iso: relayIso(keepAliveEndsAt),
            seconds_until_keep_alive_end: relayUntil(keepAliveEndsAt, now),
            printer_power_off_at: printerPowerOffAt,
            printer_power_off_at_iso: relayIso(printerPowerOffAt),
            seconds_until_printer_power_off: relayUntil(printerPowerOffAt, now),
            scheduled_turn_off_at: scheduled,
            scheduled_turn_off_at_iso: relayIso(scheduled),
            seconds_until_turn_off: relayUntil(scheduled, now),
            next_step: nextStep,
            next_step_at: nextStepAt,
            next_step_at_iso: relayIso(nextStepAt),
            seconds_until_next_step: relayUntil(nextStepAt, now),

            last_action: state.relay.lastAction,
            last_action_at: state.relay.lastActionAt,
            last_error: state.relay.lastError,
            last_error_at: state.relay.lastErrorAt,

            warning: RELAY_WARNING,
            warning_armed: enabled && turnOffEnabled
        };
    }

    /**
     * Answer a webhook fired by hand, and say plainly that nothing was sent.
     *
     * This is the one endpoint in the whole mock where a cheerful answer would
     * be a lie with consequences. The catch-all used to return HTTP 200 with an
     * empty body, out of which the UI built a green "turn_on delivered" -- a
     * claim that a relay somewhere had switched, when there is no backend to
     * POST from and no relay to POST to.
     *
     * So the report says the request was not confirmed (`success: false`), that
     * nothing came back (`response_status: null`) and that the mains state is
     * therefore unknown. Those three fields are what relayFireOutcome() in
     * relay.js composes its line from, and they produce "turn_on not confirmed"
     * in the caution colour rather than a green delivery, with the demo's own
     * explanation behind the "What the server said" toggle.
     *
     * The simulated mains state does move, because that is the mock's own
     * bookkeeping rather than a claim about a webhook: turn_off puts the demo
     * printer back to sleep so the cold start can be watched again, and turn_on
     * wakes it as the print path's own webhook does.
     *
     * @param {string} action - 'turn_on' | 'turn_off'
     */
    function relayPowerSend(action) {
        const s = state.settings;
        if (action !== 'turn_on' && action !== 'turn_off') {
            return jsonResponse({
                message: "Unknown relay action '" + String(action) + "'. It must be " +
                    "'turn_on' or 'turn_off'."
            }, 400);
        }
        if (!s.relay_webhook_enabled) {
            return jsonResponse({
                message: 'Relay power control is switched off, so nothing was sent. ' +
                    'Switch relay_webhook_enabled on before sending a webhook by hand.'
            }, 400);
        }

        const url = action === 'turn_off'
            ? (String(s.relay_webhook_turn_off_url || '').trim() ||
               String(s.relay_webhook_turn_on_url || '').trim())
            : String(s.relay_webhook_turn_on_url || '').trim();
        if (!url) {
            return jsonResponse({
                message: "No relay webhook URL is configured for '" + action +
                    "', so nothing was sent."
            }, 400);
        }

        const sentAt = new Date().toISOString();
        // The exact body the real app would have POSTed. Reported for the same
        // reason the server reports it: "what would you actually send" is the
        // first question anyone evaluating this feature has.
        const payload = {
            action: action,
            source: 'brother_ql_app',
            printer_uri: s.printer_uri || '',
            printer_model: s.printer_model || '',
            timestamp: sentAt
        };

        // Carries no URL, deliberately: relay.js drops a reason that names one
        // (the address is in the field right above the line) and would fall
        // back to something vaguer.
        const why = 'nothing was sent, because the demo has no backend and no relay';

        state.relay.lastError = 'The ' + action + ' webhook was simulated: ' + why + '.';
        state.relay.lastErrorAt = sentAt;
        state.relay.printerPowered = (action === 'turn_on');

        return jsonResponse({
            success: false,
            action: action,
            url: url,
            payload: payload,
            authorization_sent: false,
            response_status: null,
            sent_at: sentAt,
            mains_power: 'unknown',
            message: 'The ' + action + ' webhook was simulated, not sent. On a real ' +
                'install this is one POST of the body above to ' + url + ', and the ' +
                'line here would carry the HTTP status the relay answered with. ' +
                (action === 'turn_off'
                    ? 'The demo printer is now treated as switched off, so the next ' +
                      'print shows the whole switch-on sequence again.'
                    : 'The demo printer is now treated as switched on, so the next ' +
                      'print goes straight to the wire.'),
            error: why,
            schedule_changed: false,
            scheduled_turn_off_at: relayPowerStatus().scheduled_turn_off_at
        });
    }

    /**
     * Cancel every queued job, returning how many were cancelled.
     */
    function cancelQueued() {
        let n = 0;
        state.jobs.forEach(j => {
            if (j.status === 'queued') {
                j.status = 'cancelled';
                j.finished_at = new Date().toISOString();
                setJobActivity(j, null);
                if (state.queue.activityJobId === j.id) {
                    state.queue.activityJobId = null;
                }
                n += 1;
            }
        });
        recountQueue();
        return n;
    }

    /**
     * Recompute the queued/printing counts from the job list.
     */
    function recountQueue() {
        state.queue.queued = state.jobs.filter(j => j.status === 'queued').length;
        state.queue.printing = state.jobs.filter(j => j.status === 'printing').length;
    }

    // ===================== fetch patch =====================

    const realFetch = window.fetch.bind(window);

    window.fetch = async function (input, init) {
        let url;
        let method;
        if (input instanceof Request) {
            url = input.url;
            method = (init && init.method) || input.method || 'GET';
        } else {
            url = String(input);
            method = (init && init.method) || 'GET';
        }

        // Only intercept our API; everything else (fonts, CDN, ...) passes through.
        if (url.indexOf('/api/v1/') === -1) {
            return realFetch(input, init);
        }

        const pathname = (function () {
            try {
                return new URL(url, location.href).pathname;
            } catch (e) {
                return url.split('?')[0];
            }
        })();

        const body = await parseBody(init, input instanceof Request ? input : null);

        await delay();

        const upper = String(method).toUpperCase();
        return route(upper, pathname, body);
    };

    // ===================== Demo banner + link fix-up =====================

    function injectBanner() {
        if (document.getElementById('demo-banner')) return;
        const banner = document.createElement('div');
        banner.id = 'demo-banner';
        banner.className = 'demo-banner';
        // Two things worth saying, and the second one is the one people
        // otherwise discover by being surprised: the previews here are drawn in
        // the browser, because the true-to-print ones are rendered by the
        // backend this demo does not have. They are close, not identical.
        banner.innerHTML =
            '<span class="demo-banner-text">' +
            '<i class="bi bi-info-circle-fill"></i> ' +
            'Demo mode: no printer is connected and every action is simulated. ' +
            'Previews are drawn in the browser, so some labels render slightly ' +
            'differently here than in the real app, where the backend renders ' +
            'them exactly as they print.' +
            '</span>' +
            '<button type="button" class="demo-banner-close" aria-label="Dismiss demo notice">' +
            '<i class="bi bi-x-lg"></i></button>';

        const main = document.querySelector('.main');
        if (main) {
            main.insertBefore(banner, main.firstChild);
        } else {
            document.body.insertBefore(banner, document.body.firstChild);
        }

        const close = banner.querySelector('.demo-banner-close');
        if (close) {
            close.addEventListener('click', () => banner.remove());
        }
    }

    function fixApiDocsLink() {
        // The "API Documentation" link points at /api/v1/ui/, which is served by
        // the Python backend and therefore missing on a static host.
        //
        // The published demo has a replacement: the Pages workflow renders the
        // specification into /api/ with Redoc, so the link goes there. Anywhere
        // else -- a local ?demo run against these files -- that folder does not
        // exist, and the repository beats a 404.
        const onPages = location.hostname.endsWith('github.io');
        const href = onPages ? 'api/' : 'https://github.com/Dodoooh/brother_ql_app';
        const title = onPages ? 'API reference' : 'Project on GitHub';
        document.querySelectorAll('a[href="/api/v1/ui/"]').forEach(a => {
            a.setAttribute('href', href);
            a.setAttribute('title', title);
        });
    }

    // Easter egg: any action on the seeded Rick Astley job (open, reprint or
    // delete) rickrolls the user instead of hitting the mocked endpoints.
    // Capture-phase so it runs before the app's own queue handlers, which it
    // then suppresses (so the job is never actually opened/reprinted/deleted).
    const RICKROLL_URL = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ';
    const RICKROLL_ACTIONS = ['open', 'reprint', 'delete'];
    function installRickroll() {
        document.addEventListener('click', function (e) {
            const btn = e.target.closest && e.target.closest('[data-action]');
            if (!btn) return;
            const action = btn.getAttribute('data-action');
            if (btn.getAttribute('data-job-id') === 'rickroll' &&
                RICKROLL_ACTIONS.indexOf(action) !== -1) {
                e.preventDefault();
                e.stopImmediatePropagation();
                window.open(RICKROLL_URL, '_blank', 'noopener');
            }
        }, true);
    }

    // PDF printing needs the Python backend (server-side pdfium rendering), so it
    // cannot work on a static host. The nav tab stays normal; inside the panel we
    // add a short notice and disable only the file upload (the rest stays
    // interactive so the UI can still be explored).
    function markPdfUnavailable() {
        const form = document.getElementById('pdf-form');
        if (form && !form.previousElementSibling?.classList?.contains('demo-notice')) {
            const notice = document.createElement('div');
            notice.className = 'demo-notice';
            notice.innerHTML =
                '<i class="bi bi-info-circle-fill"></i> ' +
                'PDF rendering runs on the local backend and is disabled in this demo. ' +
                'Run the app locally to print PDFs.';
            form.parentNode.insertBefore(notice, form);
        }
        const input = document.getElementById('pdf-input');
        if (input) input.disabled = true;
    }

    function onReady() {
        injectBanner();
        fixApiDocsLink();
        installRickroll();
        markPdfUnavailable();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', onReady);
    } else {
        onReady();
    }
})();
