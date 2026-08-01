// Brother QL Printer App - Core Functionality

document.addEventListener('DOMContentLoaded', () => {
    // Initialize the application
    initApp();
    
    // Check printer status on load
    setTimeout(checkPrinterStatus, 1000);
    
    // Set up automatic printer status check every 30 seconds
    setInterval(checkPrinterStatus, 30000);
});

/**
 * Initialize the application
 */
function initApp() {
    // Load settings
    loadSettings();
    
    // Set up event listeners
    setupEventListeners();
    
    // Initialize theme
    initTheme();
    
    // Initialize QR code library
    initQRCode();
    
    // Initialize preview elements
    initPreviewElements();

    // Check for a shared file hand-off (?share=token&type=pdf|image)
    handleSharedFile();

    console.log('Brother QL Printer App initialized');
}

/**
 * Map a compose tab target id to the server-preview mode, or null if the tab
 * has no server-rendered preview (e.g. PDF, which has its own preview).
 * @param {string} targetId - e.g. '#text-panel'
 */
function previewModeForTarget(targetId) {
    switch (targetId) {
        case '#text-panel': return 'text';
        case '#qrcode-panel': return 'qrcode';
        case '#label-panel': return 'label';
        case '#image-panel': return 'image';
        default: return null;
    }
}

/**
 * Determine the server-preview mode of the currently active compose tab.
 */
function getActiveComposeMode() {
    const activePane = document.querySelector('#composeContent > .tab-pane.active');
    return activePane ? previewModeForTarget('#' + activePane.id) : null;
}

/**
 * Set up event listeners for the application
 */
function setupEventListeners() {
    // Theme toggle
    const themeToggle = document.getElementById('theme-toggle');
    if (themeToggle) {
        themeToggle.addEventListener('click', toggleTheme);
    }
    
    // Check printer status (both buttons)
    const checkStatusButton = document.getElementById('check-status');
    const navbarCheckStatusButton = document.getElementById('navbar-check-status');
    
    if (checkStatusButton) {
        checkStatusButton.addEventListener('click', () => {
            const statusModal = new bootstrap.Modal(document.getElementById('statusModal'));
            statusModal.show();
            checkPrinterStatus();
        });
    }
    
    if (navbarCheckStatusButton) {
        navbarCheckStatusButton.addEventListener('click', () => {
            const statusModal = new bootstrap.Modal(document.getElementById('statusModal'));
            statusModal.show();
            checkPrinterStatus();
        });
    }

    // Always-visible keep-alive toggle in the navbar
    const navbarKeepAlive = document.getElementById('navbar-keepalive');
    if (navbarKeepAlive && typeof toggleKeepAliveFromNavbar === 'function') {
        navbarKeepAlive.addEventListener('click', toggleKeepAliveFromNavbar);
    }
    
    // Settings card toggle
    const settingsHeader = document.querySelector('.settings-card .card-header');
    if (settingsHeader) {
        settingsHeader.addEventListener('click', function() {
            const expanded = this.getAttribute('aria-expanded') === 'true';
            this.setAttribute('aria-expanded', !expanded);
        });
    }
    
    // Tab change events for preview updates
    const tabEls = document.querySelectorAll('button[data-bs-toggle="tab"]');
    tabEls.forEach(tabEl => {
        tabEl.addEventListener('shown.bs.tab', event => {
            const targetId = event.target.getAttribute('data-bs-target');

            // The sidebar splits nav items into two groups (Compose / System),
            // which confuses Bootstrap's automatic deactivation and can leave the
            // previously active pane visible (e.g. the Text form showing above
            // Settings). Enforce exactly one active pane + one active nav item.
            document.querySelectorAll('#composeContent > .tab-pane').forEach(p => {
                if ('#' + p.id !== targetId) p.classList.remove('show', 'active');
            });
            document.querySelectorAll('[data-bs-toggle="tab"]').forEach(n => {
                const isActive = n.getAttribute('data-bs-target') === targetId;
                n.classList.toggle('active', isActive);
                n.setAttribute('aria-selected', isActive ? 'true' : 'false');
            });

            // Reset the shared preview so each tab shows ONLY its own content
            // (or the placeholder). Without this, content rendered for one tab
            // (e.g. typed text) lingers in the preview on every other tab.
            ['preview-text', 'preview-image', 'preview-qrcode', 'preview-label', 'pdf-preview'].forEach(id => {
                const el = document.getElementById(id);
                if (el) el.classList.add('d-none');
            });
            const previewPlaceholder = document.getElementById('preview-placeholder');
            if (previewPlaceholder) previewPlaceholder.classList.remove('d-none');

            // Drop any stale server preview from the previous tab; the active
            // tab's render is (re-)requested below if it has content.
            if (typeof clearServerPreview === 'function') clearServerPreview();

            // Re-render the preview for the now-active tab (empty -> placeholder).
            if (targetId === '#text-panel') {
                updateTextPreview();
            } else if (targetId === '#image-panel') {
                // The image change handler only fires on file selection, so
                // re-show the already-loaded image (if any) on tab switch.
                const previewImage = document.getElementById('preview-image');
                const imageInput = document.getElementById('image-input');
                if (previewImage && previewImage.dataset.originalSrc &&
                    imageInput && imageInput.files && imageInput.files.length > 0) {
                    previewImage.classList.remove('d-none');
                    hideOtherPreviews('preview-image');
                }
            } else if (targetId === '#qrcode-panel') {
                updateQRCodePreview();
            } else if (targetId === '#label-panel') {
                updateLabelPreview();
            } else if (targetId === '#textimage-panel') {
                // The textimage change handler only fires on file selection, so
                // re-show the already-loaded image (if any) on tab switch.
                const previewImage = document.getElementById('preview-image');
                const textImageInput = document.getElementById('textimage-input');
                if (previewImage && previewImage.dataset.originalSrc &&
                    textImageInput && textImageInput.files && textImageInput.files.length > 0) {
                    previewImage.classList.remove('d-none');
                    hideOtherPreviews('preview-image');
                }
            } else if (targetId === '#pdf-panel') {
                const pdfInput = document.getElementById('pdf-input');
                if (pdfInput && pdfInput.files && pdfInput.files.length > 0) {
                    previewPdf();
                }
            }

            // Re-request the server-rendered preview for the now-active tab.
            // requestServerPreview() itself no-ops (and clears) on empty input.
            const serverMode = previewModeForTarget(targetId);
            if (serverMode && typeof requestServerPreview === 'function') {
                requestServerPreview(serverMode);
            }
        });
    });
    
    // Text print form
    const textForm = document.getElementById('text-form');
    if (textForm) {
        textForm.addEventListener('submit', handleTextPrint);
        
        // Text preview
        const textInput = document.getElementById('text-input');
        const textFontSize = document.getElementById('text-font-size');
        const textAlignment = document.getElementById('text-alignment');

        if (textInput && textFontSize && textAlignment) {
            [textInput, textFontSize, textAlignment].forEach(el => {
                el.addEventListener('input', updateTextPreview);
                // Also push a debounced server-rendered (true-to-print) preview.
                el.addEventListener('input', () => requestServerPreview('text'));
            });
        }

        // Orientation and vertical alignment only change the rendered label,
        // not the client-side mock preview, so they just refresh the
        // server-rendered one.
        ['text-orientation', 'text-vertical-alignment'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.addEventListener('change', () => requestServerPreview('text'));
        });
    }
    
    // Image print form
    const imageForm = document.getElementById('image-form');
    if (imageForm) {
        imageForm.addEventListener('submit', handleImagePrint);
    }
    
    // Image preview
    const imageInput = document.getElementById('image-input');
    if (imageInput) {
        imageInput.addEventListener('change', handleImagePreview);
        imageInput.addEventListener('change', () => requestServerPreview('image'));
    }
    const imageMode = document.getElementById('image-mode');
    if (imageMode) {
        imageMode.addEventListener('change', () => requestServerPreview('image'));
    }
    
    // QR code print form
    const qrcodeForm = document.getElementById('qrcode-form');
    if (qrcodeForm) {
        qrcodeForm.addEventListener('submit', handleQRCodePrint);
        
        // QR code preview
        const qrData = document.getElementById('qr-data');
        const qrSize = document.getElementById('qr-size');
        const qrErrorCorrection = document.getElementById('qr-error-correction');
        const qrShowText = document.getElementById('qr-show-text');
        const qrTextContent = document.getElementById('qr-text-content');
        const qrTextPosition = document.getElementById('qr-text-position');
        const qrTextFontSize = document.getElementById('qr-text-font-size');
        const qrTextAlignment = document.getElementById('qr-text-alignment');
        
        if (qrData) {
            qrData.addEventListener('input', updateQRCodePreview);
        }
        
        if (qrSize) {
            qrSize.addEventListener('input', updateQRCodePreview);
        }
        
        if (qrErrorCorrection) {
            qrErrorCorrection.addEventListener('change', updateQRCodePreview);
        }
        
        if (qrShowText) {
            qrShowText.addEventListener('change', () => {
                const qrTextOptions = document.getElementById('qr-text-options');
                if (qrTextOptions) {
                    qrTextOptions.style.display = qrShowText.checked ? 'block' : 'none';
                }
                updateQRCodePreview();
            });
        }
        
        if (qrTextContent) {
            qrTextContent.addEventListener('input', updateQRCodePreview);
        }
        
        if (qrTextPosition) {
            qrTextPosition.addEventListener('change', updateQRCodePreview);
        }
        
        if (qrTextFontSize) {
            qrTextFontSize.addEventListener('input', updateQRCodePreview);
        }
        
        if (qrTextAlignment) {
            qrTextAlignment.addEventListener('change', updateQRCodePreview);
        }

        // Any QR field change also refreshes the server-rendered preview.
        [qrData, qrSize, qrErrorCorrection, qrShowText, qrTextContent,
         qrTextPosition, qrTextFontSize, qrTextAlignment].forEach(el => {
            if (!el) return;
            const evt = (el.tagName === 'SELECT' || el.type === 'checkbox') ? 'change' : 'input';
            el.addEventListener(evt, () => requestServerPreview('qrcode'));
        });
    }
    
    // Label print form
    const labelForm = document.getElementById('label-form');
    if (labelForm) {
        labelForm.addEventListener('submit', handleLabelPrint);
        
        // Label preview
        const labelQrData = document.getElementById('label-qr-data');
        const labelQrPosition = document.getElementById('label-qr-position');
        const labelQrErrorCorrection = document.getElementById('label-qr-error-correction');
        const labelTextContent = document.getElementById('label-text-content');
        const labelTextFontSize = document.getElementById('label-text-font-size');
        const labelTextAlignment = document.getElementById('label-text-alignment');
        
        if (labelQrData) {
            labelQrData.addEventListener('input', updateLabelPreview);
        }
        
        if (labelQrPosition) {
            labelQrPosition.addEventListener('change', updateLabelPreview);
        }
        
        if (labelQrErrorCorrection) {
            labelQrErrorCorrection.addEventListener('change', updateLabelPreview);
        }
        
        if (labelTextContent) {
            labelTextContent.addEventListener('input', updateLabelPreview);
        }
        
        if (labelTextFontSize) {
            labelTextFontSize.addEventListener('input', updateLabelPreview);
        }
        
        if (labelTextAlignment) {
            labelTextAlignment.addEventListener('change', updateLabelPreview);
        }

        // Any label field change also refreshes the server-rendered preview.
        [labelQrData, labelQrPosition, labelQrErrorCorrection, labelTextContent,
         labelTextFontSize, labelTextAlignment].forEach(el => {
            if (!el) return;
            const evt = el.tagName === 'SELECT' ? 'change' : 'input';
            el.addEventListener(evt, () => requestServerPreview('label'));
        });
    }
    
    // Text + Image print form
    const textImageForm = document.getElementById('textimage-form');
    if (textImageForm) {
        textImageForm.addEventListener('submit', handleTextImagePrint);

        // Show the selected image in the shared preview on selection.
        const textImageInput = document.getElementById('textimage-input');
        if (textImageInput) {
            textImageInput.addEventListener('change', handleTextImagePreview);
        }
    }

    // PDF print form
    const pdfForm = document.getElementById('pdf-form');
    if (pdfForm) {
        pdfForm.addEventListener('submit', handlePdfPrint);

        // PDF preview triggers
        const pdfInput = document.getElementById('pdf-input');
        const pdfPages = document.getElementById('pdf-pages');

        if (pdfInput) {
            // New file selected (also fired by the shared-file hand-off) ->
            // refresh the preview immediately.
            pdfInput.addEventListener('change', previewPdf);
        }

        if (pdfPages) {
            // Debounce the page-range input so we do not POST on every keystroke.
            let pdfPagesTimer = null;
            pdfPages.addEventListener('input', () => {
                clearTimeout(pdfPagesTimer);
                pdfPagesTimer = setTimeout(previewPdf, 400);
            });
        }
    }

    // Settings form
    const settingsForm = document.getElementById('settings-form');
    if (settingsForm) {
        settingsForm.addEventListener('submit', handleSaveSettings);
    }

    // Keep-alive mode -> toggle the duration controls' visibility/state.
    const keepAliveMode = document.getElementById('keep-alive-mode');
    if (keepAliveMode && typeof updateKeepAliveModeUI === 'function') {
        keepAliveMode.addEventListener('change', updateKeepAliveModeUI);
        // Apply once on initial load (settings load also calls this).
        updateKeepAliveModeUI();
    }

    // Searchable label picker on top of the (hidden) native label dropdown.
    // It writes to #label-size and fires "change", so every listener below
    // keeps working unchanged.
    if (typeof setupLabelPicker === 'function') {
        setupLabelPicker();
    }

    // Label type -> only continuous rolls have a length to run text along, so
    // the orientation control follows the selected medium.
    const labelSize = document.getElementById('label-size');
    if (labelSize && typeof updateTextOrientationUI === 'function') {
        labelSize.addEventListener('change', updateTextOrientationUI);
        // Apply once on initial load (settings load also calls this).
        updateTextOrientationUI();
    }

    // Label type -> round die-cut media is previewed as a circle, with the
    // corners the die-cut discards marked as such.
    if (labelSize && typeof updatePreviewMediumUI === 'function') {
        labelSize.addEventListener('change', updatePreviewMediumUI);
        // Apply once on initial load (settings load also calls this).
        updatePreviewMediumUI();
    }

    // Settings fields that change the rendered output should refresh the
    // server preview of whichever compose tab is currently active.
    ['rotate', 'threshold', 'dither', 'label-size', 'printer-model'].forEach(id => {
        const el = document.getElementById(id);
        if (!el) return;
        const evt = el.tagName === 'SELECT' ? 'change' : 'input';
        el.addEventListener(evt, () => {
            const mode = getActiveComposeMode();
            if (mode) requestServerPreview(mode);
        });
    });

    // ---- Print alignment calibration (Settings summary + dialog) ----
    if (typeof setupCalibration === 'function') {
        setupCalibration();
    }

    // ---- Print queue wiring (polling + actions) ----
    setupQueue();

    // ---- Console layout wiring (sidebar drawer + preview relocation) ----
    setupConsoleLayout();
}

// Interval handle for the Queue polling loop; null while not polling.
let jobsPollTimer = null;

/**
 * Start polling the print queue (~every 1500ms) while the Queue panel is
 * active. Refreshes immediately, then on an interval. No-op if already running.
 */
function startJobsPolling() {
    if (jobsPollTimer !== null) return;
    if (typeof refreshJobs === 'function') refreshJobs();
    jobsPollTimer = setInterval(() => {
        if (typeof refreshJobs === 'function') refreshJobs();
    }, 1500);
}

/**
 * Stop the queue polling loop (called when leaving the Queue panel) so it does
 * not keep hitting the API in the background.
 */
function stopJobsPolling() {
    if (jobsPollTimer !== null) {
        clearInterval(jobsPollTimer);
        jobsPollTimer = null;
    }
}

/**
 * Wire up the print queue: start/stop polling on Queue tab show/hide, the
 * "Clear finished" button, and per-job Cancel buttons (event delegation).
 */
function setupQueue() {
    // Start/stop polling based on which tab becomes active.
    document.querySelectorAll('button[data-bs-toggle="tab"]').forEach(tabEl => {
        tabEl.addEventListener('shown.bs.tab', event => {
            const targetId = event.target.getAttribute('data-bs-target');
            if (targetId === '#queue-panel') {
                startJobsPolling();
            } else {
                stopJobsPolling();
            }
        });
    });

    // "Clear finished" button.
    const clearBtn = document.getElementById('queue-clear');
    if (clearBtn) {
        clearBtn.addEventListener('click', () => {
            if (typeof clearFinishedJobs === 'function') clearFinishedJobs();
        });
    }

    // Queue control bar: Pause/Resume toggle, Stop, Clear all.
    const pauseToggle = document.getElementById('queue-pause-toggle');
    if (pauseToggle) {
        pauseToggle.addEventListener('click', () => {
            if (typeof toggleQueuePause === 'function') toggleQueuePause();
        });
    }
    const stopBtn = document.getElementById('queue-stop');
    if (stopBtn) {
        stopBtn.addEventListener('click', () => {
            if (typeof stopQueue === 'function') stopQueue();
        });
    }
    const clearAllBtn = document.getElementById('queue-clear-all');
    if (clearAllBtn) {
        clearAllBtn.addEventListener('click', () => {
            if (typeof clearAllJobs === 'function') clearAllJobs();
        });
    }

    // Per-job action buttons (Cancel / Reprint / Open) via event delegation,
    // since the rows are re-rendered on every poll.
    const list = document.getElementById('queue-list');
    if (list) {
        list.addEventListener('click', event => {
            const btn = event.target.closest('[data-action]');
            if (!btn) return;
            const action = btn.getAttribute('data-action');
            const jobId = btn.getAttribute('data-job-id');
            if (!jobId) return;
            if (action === 'cancel' && typeof cancelJob === 'function') {
                cancelJob(jobId);
            } else if (action === 'reprint' && typeof reprintJob === 'function') {
                reprintJob(jobId);
            } else if (action === 'open' && typeof openJob === 'function') {
                openJob(jobId);
            } else if (action === 'delete' && typeof deleteJob === 'function') {
                deleteJob(jobId, btn.getAttribute('data-job-status'));
            }
        });
    }

    // Keep the sidebar badge fresh from load on, even before the Queue tab is
    // first opened.
    if (typeof refreshJobs === 'function') refreshJobs();
}

/**
 * Wire up the Console layout: the responsive sidebar drawer and the relocation
 * of the single shared preview panel into whichever compose tab is active.
 */
function setupConsoleLayout() {
    // Move the shared preview panel into the active tab's mount point so it
    // always sits next to the active compose form. Settings has no mount, so
    // the preview is simply not shown there.
    const previewPanel = document.getElementById('preview-panel-host');

    function placePreview(targetId) {
        if (!previewPanel) return;
        const pane = targetId
            ? document.querySelector(targetId)
            : document.querySelector('.tab-pane.active');
        if (!pane) return;
        const mount = pane.querySelector('.preview-mount');
        if (mount) {
            mount.appendChild(previewPanel);
            previewPanel.style.display = '';
        } else {
            // No preview slot in this pane (e.g. Settings) -> hide it.
            previewPanel.style.display = 'none';
        }
    }

    // Initial placement (Text panel is active on load).
    placePreview('#text-panel');

    // Reposition on every tab switch. This runs alongside the preview-update
    // listeners already registered above.
    document.querySelectorAll('button[data-bs-toggle="tab"]').forEach(tabEl => {
        tabEl.addEventListener('shown.bs.tab', event => {
            const targetId = event.target.getAttribute('data-bs-target');
            placePreview(targetId);
        });
    });

    // Responsive off-canvas sidebar drawer.
    const rail = document.getElementById('rail');
    const railToggle = document.getElementById('rail-toggle');
    const railScrim = document.getElementById('rail-scrim');

    function openRail() {
        if (!rail) return;
        rail.classList.add('open');
        if (railScrim) railScrim.hidden = false;
        if (railToggle) railToggle.setAttribute('aria-expanded', 'true');
    }
    function closeRail() {
        if (!rail) return;
        rail.classList.remove('open');
        if (railScrim) railScrim.hidden = true;
        if (railToggle) railToggle.setAttribute('aria-expanded', 'false');
    }

    if (railToggle) {
        railToggle.addEventListener('click', () => {
            if (rail && rail.classList.contains('open')) {
                closeRail();
            } else {
                openRail();
            }
        });
    }
    if (railScrim) railScrim.addEventListener('click', closeRail);

    // Tapping a sidebar entry on mobile should close the drawer.
    document.querySelectorAll('.rail .nav-item').forEach(item => {
        item.addEventListener('click', () => {
            if (window.matchMedia('(max-width: 768px)').matches) closeRail();
        });
    });
}

/**
 * Handle a shared file hand-off via URL parameters (?share=token&type=pdf|image).
 * Fetches the shared file and loads it into the matching file input, then
 * activates the corresponding tab so the user only needs to press "Print".
 */
async function handleSharedFile() {
    const params = new URLSearchParams(location.search);
    const token = params.get('share');
    const type = params.get('type');

    if (!token) {
        return;
    }

    try {
        const response = await fetch('/api/v1/share/' + encodeURIComponent(token));
        if (!response.ok) {
            throw new Error(`Failed to load shared file: ${response.status}`);
        }

        const blob = await response.blob();

        let inputId;
        let tabId;
        let fileName;

        if (type === 'pdf') {
            inputId = 'pdf-input';
            tabId = 'pdf-tab';
            fileName = 'shared.pdf';
        } else {
            // Default to image hand-off
            inputId = 'image-input';
            tabId = 'image-tab';
            fileName = 'shared-image';
        }

        const input = document.getElementById(inputId);
        const tabBtn = document.getElementById(tabId);

        if (input) {
            const file = new File([blob], fileName, { type: blob.type });
            const dt = new DataTransfer();
            dt.items.add(file);
            input.files = dt.files;

            // Trigger change so any preview logic runs
            input.dispatchEvent(new Event('change', { bubbles: true }));
        }

        if (tabBtn) {
            if (window.bootstrap && bootstrap.Tab) {
                new bootstrap.Tab(tabBtn).show();
            } else {
                tabBtn.click();
            }
        }
    } catch (error) {
        console.error('Error loading shared file:', error);
        if (typeof showNotification === 'function') {
            showNotification(`Error loading shared file: ${error.message}`, 'error');
        }
    } finally {
        // Remove the share params so a reload does not re-trigger the hand-off
        const url = new URL(location.href);
        url.searchParams.delete('share');
        url.searchParams.delete('type');
        history.replaceState(null, '', url.pathname + url.search + url.hash);
    }
}

/**
 * Initialize theme based on user preference
 */
function initTheme() {
    const savedTheme = localStorage.getItem('theme');
    const darkQuery = window.matchMedia('(prefers-color-scheme: dark)');

    // If the user has never manually toggled (no explicit value in
    // localStorage), follow the system preference: dark when the system is
    // dark, light otherwise (light stays the fallback default).
    const useDark = savedTheme === 'dark' || (!savedTheme && darkQuery.matches);
    applyTheme(useDark);

    // Live-follow system theme changes, but ONLY while the user has not set an
    // explicit preference. A manual toggle writes to localStorage and from then
    // on overrides the auto-detection permanently.
    const onSystemThemeChange = (e) => {
        if (localStorage.getItem('theme')) return; // explicit choice wins
        applyTheme(e.matches);
    };
    if (typeof darkQuery.addEventListener === 'function') {
        darkQuery.addEventListener('change', onSystemThemeChange);
    } else if (typeof darkQuery.addListener === 'function') {
        // Safari < 14 fallback
        darkQuery.addListener(onSystemThemeChange);
    }
}

/**
 * Apply the given theme to the document and sync the toggle icon.
 * @param {boolean} isDarkMode - Whether dark mode should be active
 */
function applyTheme(isDarkMode) {
    document.body.classList.toggle('dark-mode', isDarkMode);
    updateThemeToggleIcon(isDarkMode);
}

/**
 * Toggle between light and dark theme
 */
function toggleTheme() {
    const isDarkMode = document.body.classList.toggle('dark-mode');
    localStorage.setItem('theme', isDarkMode ? 'dark' : 'light');
    updateThemeToggleIcon(isDarkMode);
}

/**
 * Update the theme toggle icon based on current theme
 * @param {boolean} isDarkMode - Whether dark mode is active
 */
function updateThemeToggleIcon(isDarkMode) {
    const themeToggle = document.getElementById('theme-toggle');
    if (themeToggle) {
        themeToggle.innerHTML = isDarkMode ? 
            '<i class="bi bi-sun-fill"></i>' : 
            '<i class="bi bi-moon-fill"></i>';
    }
}

/**
 * Initialize QR code library
 */
function initQRCode() {
    // Load QR code library if not already loaded
    if (typeof qrcode !== 'function') {
        // First, initialize placeholders
        initQRCodePlaceholders();
        
        // Then load the library
        const script = document.createElement('script');
        script.src = 'https://cdn.jsdelivr.net/npm/qrcode-generator@1.4.4/qrcode.min.js';
        script.onload = () => {
            console.log('QR code library loaded');
            // Update previews after library is loaded if there's data
            const qrData = document.getElementById('qr-data');
            const labelQrData = document.getElementById('label-qr-data');
            
            if (qrData && qrData.value.trim()) {
                updateQRCodePreview();
            }
            
            if (labelQrData && labelQrData.value.trim()) {
                updateLabelPreview();
            }
        };
        document.head.appendChild(script);
    } else {
        // If library is already loaded, initialize placeholders
        initQRCodePlaceholders();
        
        // And update previews if there's data
        const qrData = document.getElementById('qr-data');
        const labelQrData = document.getElementById('label-qr-data');
        
        if (qrData && qrData.value.trim()) {
            updateQRCodePreview();
        }
        
        if (labelQrData && labelQrData.value.trim()) {
            updateLabelPreview();
        }
    }
}
