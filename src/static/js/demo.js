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
            keep_alive_mode: 'forever',
            keep_alive_duration_seconds: 7200,
            ipp_port: 631,
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
        queue: { paused: false, queued: 0, printing: 0 }
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

    /**
     * Queue a new demo job for a print request and flip it to "done" shortly
     * after, so the UI sees it move through the queue on the next poll.
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
            can_reprint: true
        };
        state.jobs.unshift(job);
        state.queue.queued += 1;

        // Simulate processing: printing, then done.
        setTimeout(() => {
            job.status = 'printing';
            job.started_at = new Date().toISOString();
            state.queue.queued = Math.max(0, state.queue.queued - 1);
            state.queue.printing = 1;
        }, 700);
        setTimeout(() => {
            job.status = 'done';
            job.finished_at = new Date().toISOString();
            state.queue.printing = 0;
        }, 1800);

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
     */
    function queueStatus() {
        return {
            paused: state.queue.paused,
            queued: state.jobs.filter(j => j.status === 'queued').length,
            printing: state.jobs.filter(j => j.status === 'printing').length
        };
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
        // The footer "API Documentation" link points at /api/v1/ui/, which does
        // not exist on a static host. Repoint it at the project repository.
        document.querySelectorAll('a[href="/api/v1/ui/"]').forEach(a => {
            a.setAttribute('href', 'https://github.com/Dodoooh/brother_ql_app');
            a.setAttribute('title', 'Project on GitHub');
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
