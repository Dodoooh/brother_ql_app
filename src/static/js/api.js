// Brother QL Printer App - API Interactions

// Backend requires an explicit confirmation flag when printing this many
// copies or more. The UI mirrors that threshold with a confirm dialog.
const LARGE_BATCH_THRESHOLD = 10;

/**
 * Read a panel's copies value (clamped to a sane integer >= 1).
 * @param {string} copiesId - element id of the panel's copies input
 * @returns {number}
 */
function readCopies(copiesId) {
    const el = document.getElementById(copiesId);
    const value = parseInt(el && el.value, 10);
    return Number.isFinite(value) && value >= 1 ? value : 1;
}

/**
 * If the requested copies meet the large-batch threshold, ask the user to
 * confirm. Returns true when it is safe to proceed (either below threshold or
 * the user confirmed), false when the user cancelled.
 * @param {number} copies
 * @returns {Promise<boolean>}
 */
async function confirmLargeBatch(copies) {
    if (copies < LARGE_BATCH_THRESHOLD) return true;
    return confirmDialog(
        `You are about to print ${copies} copies. Print more than 10 copies?`,
        { title: 'Confirm large batch', confirmLabel: 'Print' }
    );
}

/**
 * Parse an error response body and throw an Error carrying its message. Adds a
 * clear message when the backend reports CONFIRMATION_REQUIRED.
 * @param {Response} response
 */
async function throwPrintError(response) {
    let message = `Error: ${response.status}`;
    try {
        const errorData = await response.json();
        if (response.status === 400 && errorData.code === 'CONFIRMATION_REQUIRED') {
            throw new Error(errorData.message || 'Confirmation required for this many copies');
        }
        message = errorData.message || message;
    } catch (e) {
        if (e instanceof Error && e.message && response.status === 400) throw e;
        // Non-JSON body: keep the generic message.
    }
    throw new Error(message);
}

/**
 * Load settings from the API
 */
async function loadSettings() {
    try {
        const response = await fetch('/api/v1/settings');
        if (!response.ok) {
            throw new Error(`Failed to load settings: ${response.status}`);
        }
        
        const settings = await response.json();
        
        // Populate settings form
        document.getElementById('printer-uri').value = settings.printer_uri || '';
        document.getElementById('printer-model').value = settings.printer_model || '';
        document.getElementById('label-size').value = settings.label_size || '62';
        document.getElementById('text-font-size').value = settings.font_size || '50';
        document.getElementById('text-alignment').value = settings.alignment || 'left';
        document.getElementById('rotate').value = settings.rotate || '0';
        document.getElementById('threshold').value = settings.threshold || '70';
        document.getElementById('dither').value = settings.dither ? 'true' : 'false';
        document.getElementById('red').value = settings.red ? 'true' : 'false';
        // Apply the saved copies/cut defaults to every compose panel.
        const defaultCopies = settings.copies || 1;
        const defaultCutMode = settings.cut_mode || 'each';
        ['copies', 'copies-image', 'copies-qrcode', 'copies-label', 'copies-pdf', 'copies-textimage'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.value = defaultCopies;
        });
        ['cut-mode', 'cut-mode-image', 'cut-mode-qrcode', 'cut-mode-label', 'cut-mode-pdf', 'cut-mode-textimage'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.value = defaultCutMode;
        });
        document.getElementById('dpi-600').value = settings.dpi_600 ? 'true' : 'false';
        document.getElementById('keep-alive-enabled').value = settings.keep_alive_enabled ? 'true' : 'false';
        document.getElementById('keep-alive-interval').value = settings.keep_alive_interval || '60';

        // Keep alive mode + duration (derive a sensible value+unit for display)
        document.getElementById('keep-alive-mode').value = settings.keep_alive_mode || 'forever';
        applyKeepAliveDuration(settings.keep_alive_duration_seconds);
        // Reflect the current mode in the duration controls' visibility/state.
        updateKeepAliveModeUI();

        // Also check the current keep alive status
        loadKeepAliveStatus();
        
        console.log('Settings loaded successfully');
    } catch (error) {
        console.error('Error loading settings:', error);
        showNotification('Error loading settings', 'error');
    }
}

/**
 * Derive a sensible value + unit from a keep_alive_duration_seconds value and
 * populate the duration input/unit fields. Prefers hours when the value divides
 * evenly by 3600, otherwise falls back to minutes.
 * @param {number} seconds
 */
function applyKeepAliveDuration(seconds) {
    const valueEl = document.getElementById('keep-alive-duration-value');
    const unitEl = document.getElementById('keep-alive-duration-unit');
    if (!valueEl || !unitEl) return;

    const total = (typeof seconds === 'number' && seconds >= 0) ? seconds : 7200;

    if (total > 0 && total % 3600 === 0) {
        valueEl.value = String(total / 3600);
        unitEl.value = 'hours';
    } else {
        valueEl.value = String(Math.round(total / 60));
        unitEl.value = 'minutes';
    }
}

/**
 * Toggle the visibility / disabled state of the keep-alive duration controls
 * based on the selected keep-alive mode. The duration only applies in "timed"
 * mode, so it is hidden + disabled in "forever" mode.
 */
function updateKeepAliveModeUI() {
    const modeEl = document.getElementById('keep-alive-mode');
    const durationField = document.getElementById('keep-alive-duration-field');
    const valueEl = document.getElementById('keep-alive-duration-value');
    const unitEl = document.getElementById('keep-alive-duration-unit');
    if (!modeEl || !durationField) return;

    const timed = modeEl.value === 'timed';
    durationField.style.display = timed ? '' : 'none';
    if (valueEl) valueEl.disabled = !timed;
    if (unitEl) unitEl.disabled = !timed;
}

/**
 * Load keep alive status from the API
 */
async function loadKeepAliveStatus() {
    try {
        const response = await fetch('/api/v1/printers/keep-alive');
        if (!response.ok) {
            throw new Error(`Failed to load keep alive status: ${response.status}`);
        }
        
        const status = await response.json();

        // Reflect state in the always-visible navbar pill
        updateKeepAlivePill(status);

        // Update status indicator
        const keepAliveEnabled = document.getElementById('keep-alive-enabled');
        const statusText = status.running ?
            'Keep alive is active and running' :
            'Keep alive is not running';
        
        // Add a status indicator below the keep alive controls
        const statusIndicator = document.createElement('div');
        statusIndicator.id = 'keep-alive-status';
        statusIndicator.className = status.running ? 'text-success mt-2' : 'text-muted mt-2';
        statusIndicator.innerHTML = `<i class="bi ${status.running ? 'bi-check-circle-fill' : 'bi-x-circle-fill'} me-1"></i> ${statusText}`;
        
        // Replace existing status indicator if it exists
        const existingStatus = document.getElementById('keep-alive-status');
        if (existingStatus) {
            existingStatus.replaceWith(statusIndicator);
        } else {
            // Find the parent element to append the status indicator
            const keepAliveParent = keepAliveEnabled.closest('.col-md-6');
            keepAliveParent.appendChild(statusIndicator);
        }
        
        console.log('Keep alive status loaded successfully', status);
    } catch (error) {
        console.error('Error loading keep alive status:', error);
    }
}

/**
 * Update the always-visible navbar keep-alive pill to mirror the current state.
 * @param {{enabled?: boolean, running?: boolean}} status
 */
function updateKeepAlivePill(status) {
    const pill = document.getElementById('navbar-keepalive');
    const label = document.getElementById('keepalive-indicator');
    if (!pill || !label) return;
    const running = !!(status && status.running);
    pill.classList.toggle('ka-active', running);
    pill.setAttribute('aria-pressed', running ? 'true' : 'false');
    pill.title = running ? 'Keep alive is running — click to turn off' : 'Keep alive is off — click to turn on';
    label.textContent = running ? 'Keep Alive: On' : 'Keep Alive: Off';
}

/**
 * Toggle keep alive on/off from the navbar pill. Reuses the keep-alive interval
 * configured in Settings (falling back to 60s) and refreshes the pill afterwards.
 */
async function toggleKeepAliveFromNavbar() {
    const pill = document.getElementById('navbar-keepalive');
    if (!pill) return;
    const turnOn = !pill.classList.contains('ka-active');
    const intervalEl = document.getElementById('keep-alive-interval');
    let interval = parseInt(intervalEl && intervalEl.value, 10);
    if (!Number.isFinite(interval) || interval < 10) interval = 60;

    pill.classList.add('busy');
    try {
        await updateKeepAlive(turnOn, interval);
        // Keep the Settings dropdown in sync if it is present in the DOM
        const enabledSel = document.getElementById('keep-alive-enabled');
        if (enabledSel) enabledSel.value = turnOn ? 'true' : 'false';
    } finally {
        pill.classList.remove('busy');
        // Re-sync the pill with the real server state (covers failed toggles too)
        loadKeepAliveStatus();
    }
}

/**
 * Check printer status
 */
async function checkPrinterStatus() {
    const statusResult = document.getElementById('status-result');
    const statusIndicator = document.getElementById('status-indicator');
    const navbarStatusBtn = document.getElementById('navbar-check-status');
    
    // Show loading state
    if (statusResult) {
        statusResult.innerHTML = '<div class="d-flex justify-content-center"><div class="spinner-border text-primary" role="status"><span class="visually-hidden">Loading...</span></div></div>';
    }
    if (statusIndicator) {
        statusIndicator.textContent = 'Checking...';
    }
    
    try {
        const printerUri = document.getElementById('printer-uri').value;
        const printerModel = document.getElementById('printer-model').value;
        
        if (!printerUri || !printerModel) {
            throw new Error('Printer URI and model are required');
        }
        
        const response = await fetch('/api/v1/printers/status', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                printer_uri: printerUri,
                printer_model: printerModel
            })
        });
        
        if (!response.ok) {
            const errorData = await response.json();
            throw new Error(errorData.message || `Error: ${response.status}`);
        }
        
        const data = await response.json();
        
        if (data.available) {
            // Update status result in modal
            if (statusResult) {
                statusResult.innerHTML = `
                    <div class="alert alert-success">
                        <div class="d-flex align-items-center">
                            <i class="bi bi-check-circle-fill me-2 fs-4"></i>
                            <div>
                                <strong>Printer is available</strong><br>
                                ${data.status}
                            </div>
                        </div>
                    </div>
                `;
            }
            
            // Update navbar status indicator
            if (statusIndicator) {
                statusIndicator.textContent = 'Online';
            }
            if (navbarStatusBtn) {
                navbarStatusBtn.classList.remove('offline');
                navbarStatusBtn.classList.add('online');
            }
        } else {
            // Update status result in modal
            if (statusResult) {
                statusResult.innerHTML = `
                    <div class="alert alert-warning">
                        <div class="d-flex align-items-center">
                            <i class="bi bi-exclamation-triangle-fill me-2 fs-4"></i>
                            <div>
                                <strong>Printer is not available</strong><br>
                                ${data.status}
                            </div>
                        </div>
                    </div>
                `;
            }
            
            // Update navbar status indicator
            if (statusIndicator) {
                statusIndicator.textContent = 'Offline';
            }
            if (navbarStatusBtn) {
                navbarStatusBtn.classList.remove('online');
                navbarStatusBtn.classList.add('offline');
            }
        }
    } catch (error) {
        console.error('Error checking printer status:', error);
        
        // Update status result in modal
        if (statusResult) {
            // Check if it's a connection error
            const isConnectionError = error.message.includes('Connection refused');
            
            statusResult.innerHTML = `
                <div class="alert alert-danger">
                    <div class="d-flex align-items-center">
                        <i class="bi bi-x-circle-fill me-2 fs-4"></i>
                        <div>
                            <strong>${isConnectionError ? 'Connection Error' : 'Error'}</strong><br>
                            ${error.message}
                            ${isConnectionError ? '<br><br>Please check that:<ul class="mb-0 ps-3"><li>The printer is turned on</li><li>The printer is connected to the network</li><li>The IP address is correct</li></ul>' : ''}
                        </div>
                    </div>
                </div>
            `;
        }
        
        // Update navbar status indicator
        if (statusIndicator) {
            statusIndicator.textContent = 'Error';
        }
        if (navbarStatusBtn) {
            navbarStatusBtn.classList.remove('online');
            navbarStatusBtn.classList.add('offline');
        }
    }
}

/**
 * Handle text print form submission
 * @param {Event} event - Form submit event
 */
async function handleTextPrint(event) {
    event.preventDefault();
    
    try {
        const text = document.getElementById('text-input').value;
        const fontSize = document.getElementById('text-font-size').value;
        const alignment = document.getElementById('text-alignment').value;
        
        // Get printer settings
        const printerUri = document.getElementById('printer-uri').value;
        const printerModel = document.getElementById('printer-model').value;
        const labelSize = document.getElementById('label-size').value;
        const rotate = document.getElementById('rotate').value;
        const threshold = document.getElementById('threshold').value;
        const dither = document.getElementById('dither').value === 'true';
        const red = document.getElementById('red').value === 'true';
        
        if (!text) {
            throw new Error('Text is required');
        }

        if (!printerUri || !printerModel || !labelSize) {
            throw new Error('Printer settings are incomplete');
        }

        const copies = readCopies('copies');
        if (!await confirmLargeBatch(copies)) return;

        // Show loading state
        const submitBtn = event.target.querySelector('button[type="submit"]');
        const originalBtnText = submitBtn.innerHTML;
        submitBtn.disabled = true;
        submitBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Printing...';

        const requestBody = {
            text: text,
            settings: {
                printer_uri: printerUri,
                printer_model: printerModel,
                label_size: labelSize,
                font_size: parseInt(fontSize),
                alignment: alignment,
                rotate: parseInt(rotate),
                threshold: parseFloat(threshold),
                dither: dither,
                red: red,
                copies: copies,
                cut_mode: document.getElementById('cut-mode').value,
                dpi_600: document.getElementById('dpi-600').value === 'true'
            }
        };
        if (copies >= LARGE_BATCH_THRESHOLD) {
            requestBody.confirm_large_batch = true;
        }

        const response = await fetch('/api/v1/text/print', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(requestBody)
        });

        // Reset button state
        submitBtn.disabled = false;
        submitBtn.innerHTML = originalBtnText;

        if (!response.ok) {
            await throwPrintError(response);
        }

        const data = await response.json();

        showNotification('Added to print queue', 'success');
        console.log('Print result:', data);
        if (typeof refreshJobs === 'function') refreshJobs();
    } catch (error) {
        console.error('Error printing text:', error);
        showNotification(`Error printing text: ${error.message}`, 'error');
    }
}

/**
 * Handle image print form submission
 * @param {Event} event - Form submit event
 */
async function handleImagePrint(event) {
    event.preventDefault();
    
    try {
        const imageInput = document.getElementById('image-input');
        const imageMode = document.getElementById('image-mode');
        
        if (!imageInput.files || imageInput.files.length === 0) {
            throw new Error('No image selected');
        }
        
        // Get printer settings
        const printerUri = document.getElementById('printer-uri').value;
        const printerModel = document.getElementById('printer-model').value;
        const labelSize = document.getElementById('label-size').value;
        const rotate = document.getElementById('rotate').value;
        const threshold = document.getElementById('threshold').value;
        
        // Determine dithering based on image mode
        let dither = document.getElementById('dither').value === 'true';
        if (imageMode.value === 'bw-dither') {
            dither = true;
        } else if (imageMode.value === 'bw') {
            dither = false;
        }
        
        const red = document.getElementById('red').value === 'true';
        
        if (!printerUri || !printerModel || !labelSize) {
            throw new Error('Printer settings are incomplete');
        }

        const copies = readCopies('copies-image');
        if (!await confirmLargeBatch(copies)) return;

        // Show loading state
        const submitBtn = event.target.querySelector('button[type="submit"]');
        const originalBtnText = submitBtn.innerHTML;
        submitBtn.disabled = true;
        submitBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Printing...';

        const formData = new FormData();
        formData.append('image', imageInput.files[0]);
        formData.append('settings', JSON.stringify({
            printer_uri: printerUri,
            printer_model: printerModel,
            label_size: labelSize,
            rotate: parseInt(rotate),
            threshold: parseFloat(threshold),
            dither: dither,
            red: red,
            copies: copies,
            cut_mode: document.getElementById('cut-mode-image').value,
            dpi_600: document.getElementById('dpi-600').value === 'true',
            image_mode: imageMode.value
        }));
        if (copies >= LARGE_BATCH_THRESHOLD) {
            formData.append('confirm_large_batch', 'true');
        }

        const response = await fetch('/api/v1/image/print', {
            method: 'POST',
            body: formData
        });

        // Reset button state
        submitBtn.disabled = false;
        submitBtn.innerHTML = originalBtnText;

        if (!response.ok) {
            await throwPrintError(response);
        }

        const data = await response.json();

        showNotification('Added to print queue', 'success');
        console.log('Print result:', data);
        if (typeof refreshJobs === 'function') refreshJobs();
    } catch (error) {
        console.error('Error printing image:', error);
        showNotification(`Error printing image: ${error.message}`, 'error');
    }
}

/**
 * Handle PDF print form submission
 * @param {Event} event - Form submit event
 */
async function handlePdfPrint(event) {
    event.preventDefault();

    try {
        const pdfInput = document.getElementById('pdf-input');

        if (!pdfInput.files || pdfInput.files.length === 0) {
            throw new Error('No PDF selected');
        }

        // Get printer settings
        const printerUri = document.getElementById('printer-uri').value;
        const printerModel = document.getElementById('printer-model').value;
        const labelSize = document.getElementById('label-size').value;
        const rotate = document.getElementById('rotate').value;
        const threshold = document.getElementById('threshold').value;
        const dither = document.getElementById('dither').value === 'true';
        const red = document.getElementById('red').value === 'true';

        if (!printerUri || !printerModel || !labelSize) {
            throw new Error('Printer settings are incomplete');
        }

        const pages = document.getElementById('pdf-pages').value;
        const scaleMode = document.getElementById('pdf-scale-mode').value;

        const copies = readCopies('copies-pdf');
        if (!await confirmLargeBatch(copies)) return;

        // Show loading state
        const submitBtn = event.target.querySelector('button[type="submit"]');
        const originalBtnText = submitBtn.innerHTML;
        submitBtn.disabled = true;
        submitBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Printing...';

        const formData = new FormData();
        formData.append('file', pdfInput.files[0]);
        formData.append('settings', JSON.stringify({
            printer_uri: printerUri,
            printer_model: printerModel,
            label_size: labelSize,
            rotate: parseInt(rotate),
            threshold: parseFloat(threshold),
            dither: dither,
            red: red,
            copies: copies,
            cut_mode: document.getElementById('cut-mode-pdf').value,
            dpi_600: document.getElementById('dpi-600').value === 'true'
        }));
        formData.append('pages', pages);
        formData.append('scale_mode', scaleMode);
        if (copies >= LARGE_BATCH_THRESHOLD) {
            formData.append('confirm_large_batch', 'true');
        }

        const response = await fetch('/api/v1/pdf/print', {
            method: 'POST',
            body: formData
        });

        // Reset button state
        submitBtn.disabled = false;
        submitBtn.innerHTML = originalBtnText;

        if (!response.ok) {
            await throwPrintError(response);
        }

        const data = await response.json();

        showNotification('Added to print queue', 'success');
        console.log('Print result:', data);
        if (typeof refreshJobs === 'function') refreshJobs();
    } catch (error) {
        console.error('Error printing PDF:', error);
        showNotification(`Error printing PDF: ${error.message}`, 'error');
    }
}

// Holds the AbortController of the in-flight PDF preview request so that
// rapidly firing triggers (file change + debounced page input) cannot leave
// stale results on screen.
let pdfPreviewController = null;

/**
 * Hide the PDF preview container and clear its contents. Optionally restore the
 * placeholder if no other preview is currently visible.
 */
function clearPdfPreview() {
    const pdfPreview = document.getElementById('pdf-preview');
    const pdfPreviewPages = document.getElementById('pdf-preview-pages');
    const pdfPreviewNotice = document.getElementById('pdf-preview-notice');
    const previewPlaceholder = document.getElementById('preview-placeholder');

    if (pdfPreviewPages) pdfPreviewPages.innerHTML = '';
    if (pdfPreviewNotice) {
        pdfPreviewNotice.textContent = '';
        pdfPreviewNotice.classList.add('d-none');
    }
    if (pdfPreview) pdfPreview.classList.add('d-none');

    // Restore the placeholder if nothing else is shown.
    if (previewPlaceholder &&
        typeof areAllPreviewsEmpty === 'function' &&
        areAllPreviewsEmpty()) {
        previewPlaceholder.classList.remove('d-none');
    }
}

/**
 * Render a server-side PDF preview for the currently selected file.
 * Reads the file from #pdf-input and the page selection from #pdf-pages,
 * POSTs them to /api/v1/pdf/preview and renders the returned thumbnails.
 */
async function previewPdf() {
    const pdfInput = document.getElementById('pdf-input');
    const pdfPreview = document.getElementById('pdf-preview');
    const pdfPreviewPages = document.getElementById('pdf-preview-pages');
    const pdfPreviewNotice = document.getElementById('pdf-preview-notice');
    const previewPlaceholder = document.getElementById('preview-placeholder');

    if (!pdfInput || !pdfPreview || !pdfPreviewPages) return;

    // No file -> clear and hide the preview.
    if (!pdfInput.files || pdfInput.files.length === 0) {
        clearPdfPreview();
        return;
    }

    const pages = document.getElementById('pdf-pages')
        ? document.getElementById('pdf-pages').value
        : '';

    // Abort any preview request still in flight so its (older) response can be
    // ignored and never overwrites a newer one.
    if (pdfPreviewController) {
        pdfPreviewController.abort();
    }
    const controller = new AbortController();
    pdfPreviewController = controller;

    // Loading state.
    pdfPreviewPages.innerHTML =
        '<div class="d-flex justify-content-center py-4">' +
        '<div class="spinner-border text-primary" role="status">' +
        '<span class="visually-hidden">Loading...</span></div></div>';
    if (pdfPreviewNotice) {
        pdfPreviewNotice.textContent = '';
        pdfPreviewNotice.classList.add('d-none');
    }
    pdfPreview.classList.remove('d-none');
    if (previewPlaceholder) previewPlaceholder.classList.add('d-none');
    // Hide the other previews while showing the PDF preview.
    if (typeof hideOtherPreviews === 'function') {
        hideOtherPreviews('pdf-preview');
    }

    try {
        const formData = new FormData();
        formData.append('file', pdfInput.files[0]);
        formData.append('pages', pages || '');

        const response = await fetch('/api/v1/pdf/preview', {
            method: 'POST',
            body: formData,
            signal: controller.signal
        });

        // A newer request started while this one was running: ignore this result.
        if (pdfPreviewController !== controller) return;

        if (!response.ok) {
            let message = `Error: ${response.status}`;
            try {
                const errorData = await response.json();
                message = errorData.message || message;
            } catch (e) {
                // Response body was not JSON; keep the generic message.
            }
            clearPdfPreview();
            showNotification(`PDF preview error: ${message}`, 'error');
            return;
        }

        const data = await response.json();

        // Render thumbnails.
        pdfPreviewPages.innerHTML = '';

        const previews = Array.isArray(data.previews) ? data.previews : [];
        previews.forEach(preview => {
            const wrapper = document.createElement('div');
            wrapper.className = 'pdf-preview-page text-center';

            const label = document.createElement('div');
            label.className = 'pdf-preview-page-label text-muted small mb-1';
            label.textContent = `Page ${preview.page}`;

            const img = document.createElement('img');
            img.className = 'pdf-preview-thumb img-fluid border rounded';
            img.alt = `Page ${preview.page}`;
            img.src = preview.image;
            img.style.maxWidth = '100%';

            wrapper.appendChild(label);
            wrapper.appendChild(img);
            pdfPreviewPages.appendChild(wrapper);
        });

        // Truncation notice.
        if (pdfPreviewNotice) {
            if (data.truncated) {
                const shown = previews.length;
                const total = data.total_pages != null ? data.total_pages : shown;
                pdfPreviewNotice.textContent =
                    `Showing first ${shown} of ${total} pages`;
                pdfPreviewNotice.classList.remove('d-none');
            } else {
                pdfPreviewNotice.textContent = '';
                pdfPreviewNotice.classList.add('d-none');
            }
        }

        if (previews.length === 0) {
            // Nothing to show -> fall back to a clean/hidden state.
            clearPdfPreview();
            return;
        }

        pdfPreview.classList.remove('d-none');
        if (previewPlaceholder) previewPlaceholder.classList.add('d-none');
    } catch (error) {
        // Ignore aborts triggered by a newer request.
        if (error && error.name === 'AbortError') return;
        console.error('Error generating PDF preview:', error);
        clearPdfPreview();
        showNotification(`PDF preview error: ${error.message}`, 'error');
    } finally {
        if (pdfPreviewController === controller) {
            pdfPreviewController = null;
        }
    }
}

/**
 * Handle QR code print form submission
 * @param {Event} event - Form submit event
 */
async function handleQRCodePrint(event) {
    event.preventDefault();
    
    try {
        const qrData = document.getElementById('qr-data').value;
        const qrSize = document.getElementById('qr-size').value;
        const qrErrorCorrection = document.getElementById('qr-error-correction').value;
        const qrShowText = document.getElementById('qr-show-text').checked;
        const qrTextContent = document.getElementById('qr-text-content').value;
        const qrTextPosition = document.getElementById('qr-text-position').value;
        const qrTextFontSize = document.getElementById('qr-text-font-size').value;
        const qrTextAlignment = document.getElementById('qr-text-alignment').value;
        
        // Get printer settings
        const printerUri = document.getElementById('printer-uri').value;
        const printerModel = document.getElementById('printer-model').value;
        const labelSize = document.getElementById('label-size').value;
        const rotate = document.getElementById('rotate').value;
        const threshold = document.getElementById('threshold').value;
        const dither = document.getElementById('dither').value === 'true';
        const red = document.getElementById('red').value === 'true';
        
        if (!qrData) {
            throw new Error('QR code data is required');
        }
        
        if (!printerUri || !printerModel || !labelSize) {
            throw new Error('Printer settings are incomplete');
        }

        const copies = readCopies('copies-qrcode');
        if (!await confirmLargeBatch(copies)) return;

        // Show loading state
        const submitBtn = event.target.querySelector('button[type="submit"]');
        const originalBtnText = submitBtn.innerHTML;
        submitBtn.disabled = true;
        submitBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Printing...';

        // Prepare request body with new API structure
        const requestBody = {
            qr: {
                data: qrData,
                size: parseInt(qrSize),
                error_correction: qrErrorCorrection,
                version: 1,
                box_size: 10,
                border: 4
            },
            settings: {
                printer_uri: printerUri,
                printer_model: printerModel,
                label_size: labelSize,
                rotate: parseInt(rotate),
                threshold: parseFloat(threshold),
                dither: dither,
                red: red,
                copies: copies,
                cut_mode: document.getElementById('cut-mode-qrcode').value,
                dpi_600: document.getElementById('dpi-600').value === 'true'
            }
        };

        // Add text settings if needed
        if (qrShowText && qrTextContent) {
            requestBody.text = {
                content: qrTextContent,
                position: qrTextPosition,
                font_size: parseInt(qrTextFontSize),
                alignment: qrTextAlignment
            };
        }
        if (copies >= LARGE_BATCH_THRESHOLD) {
            requestBody.confirm_large_batch = true;
        }

        const response = await fetch('/api/v1/qrcode/print', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(requestBody)
        });

        // Reset button state
        submitBtn.disabled = false;
        submitBtn.innerHTML = originalBtnText;

        if (!response.ok) {
            await throwPrintError(response);
        }

        const data = await response.json();

        showNotification('Added to print queue', 'success');
        console.log('Print result:', data);
        if (typeof refreshJobs === 'function') refreshJobs();
    } catch (error) {
        console.error('Error printing QR code:', error);
        showNotification(`Error printing QR code: ${error.message}`, 'error');
    }
}

/**
 * Handle label print form submission
 * @param {Event} event - Form submit event
 */
async function handleLabelPrint(event) {
    event.preventDefault();
    
    try {
        const labelQrData = document.getElementById('label-qr-data').value;
        const labelQrPosition = document.getElementById('label-qr-position').value;
        const labelQrErrorCorrection = document.getElementById('label-qr-error-correction').value;
        const labelTextContent = document.getElementById('label-text-content').value;
        const labelTextFontSize = document.getElementById('label-text-font-size').value;
        const labelTextAlignment = document.getElementById('label-text-alignment').value;
        
        // Get printer settings
        const printerUri = document.getElementById('printer-uri').value;
        const printerModel = document.getElementById('printer-model').value;
        const labelSize = document.getElementById('label-size').value;
        const rotate = document.getElementById('rotate').value;
        const threshold = document.getElementById('threshold').value;
        const dither = document.getElementById('dither').value === 'true';
        const red = document.getElementById('red').value === 'true';
        
        if (!labelQrData) {
            throw new Error('QR code data is required');
        }
        
        if (!labelTextContent) {
            throw new Error('Text content is required');
        }
        
        if (!printerUri || !printerModel || !labelSize) {
            throw new Error('Printer settings are incomplete');
        }

        const copies = readCopies('copies-label');
        if (!await confirmLargeBatch(copies)) return;

        // Show loading state
        const submitBtn = event.target.querySelector('button[type="submit"]');
        const originalBtnText = submitBtn.innerHTML;
        submitBtn.disabled = true;
        submitBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Printing...';

        // Prepare request body with new API structure
        const requestBody = {
            qr: {
                data: labelQrData,
                position: labelQrPosition,
                size: 400,
                error_correction: labelQrErrorCorrection || 'M',
                version: 1,
                box_size: 10,
                border: 4
            },
            text: {
                content: labelTextContent,
                font_size: parseInt(labelTextFontSize),
                alignment: labelTextAlignment
            },
            settings: {
                printer_uri: printerUri,
                printer_model: printerModel,
                label_size: labelSize,
                rotate: parseInt(rotate),
                threshold: parseFloat(threshold),
                dither: dither,
                red: red,
                copies: copies,
                cut_mode: document.getElementById('cut-mode-label').value,
                dpi_600: document.getElementById('dpi-600').value === 'true'
            }
        };
        if (copies >= LARGE_BATCH_THRESHOLD) {
            requestBody.confirm_large_batch = true;
        }

        const response = await fetch('/api/v1/label/text-qrcode', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(requestBody)
        });

        // Reset button state
        submitBtn.disabled = false;
        submitBtn.innerHTML = originalBtnText;

        if (!response.ok) {
            await throwPrintError(response);
        }
        
        const data = await response.json();
        
        showNotification('Added to print queue', 'success');
        console.log('Print result:', data);
        if (typeof refreshJobs === 'function') refreshJobs();
    } catch (error) {
        console.error('Error printing label:', error);
        showNotification(`Error printing label: ${error.message}`, 'error');
    }
}

/**
 * Handle text + image print form submission
 * @param {Event} event - Form submit event
 */
async function handleTextImagePrint(event) {
    event.preventDefault();

    try {
        const imageInput = document.getElementById('textimage-input');
        const text = document.getElementById('textimage-text').value;
        const fontSize = document.getElementById('textimage-font-size').value;
        const alignment = document.getElementById('textimage-alignment').value;
        const position = document.getElementById('textimage-position').value;

        if (!imageInput.files || imageInput.files.length === 0) {
            throw new Error('No image selected');
        }

        if (!text) {
            throw new Error('Text is required');
        }

        // Get printer settings
        const printerUri = document.getElementById('printer-uri').value;
        const printerModel = document.getElementById('printer-model').value;
        const labelSize = document.getElementById('label-size').value;
        const rotate = document.getElementById('rotate').value;
        const threshold = document.getElementById('threshold').value;
        const dither = document.getElementById('dither').value === 'true';
        const red = document.getElementById('red').value === 'true';

        if (!printerUri || !printerModel || !labelSize) {
            throw new Error('Printer settings are incomplete');
        }

        const copies = readCopies('copies-textimage');
        if (!await confirmLargeBatch(copies)) return;

        // Show loading state
        const submitBtn = event.target.querySelector('button[type="submit"]');
        const originalBtnText = submitBtn.innerHTML;
        submitBtn.disabled = true;
        submitBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Printing...';

        const formData = new FormData();
        formData.append('image', imageInput.files[0]);
        formData.append('text', text);
        formData.append('font_size', fontSize);
        formData.append('alignment', alignment);
        formData.append('position', position);
        formData.append('settings', JSON.stringify({
            printer_uri: printerUri,
            printer_model: printerModel,
            label_size: labelSize,
            rotate: parseInt(rotate),
            threshold: parseFloat(threshold),
            dither: dither,
            red: red,
            copies: copies,
            cut_mode: document.getElementById('cut-mode-textimage').value,
            dpi_600: document.getElementById('dpi-600').value === 'true'
        }));
        if (copies >= LARGE_BATCH_THRESHOLD) {
            formData.append('confirm_large_batch', 'true');
        }

        const response = await fetch('/api/v1/label/text-image', {
            method: 'POST',
            body: formData
        });

        // Reset button state
        submitBtn.disabled = false;
        submitBtn.innerHTML = originalBtnText;

        if (!response.ok) {
            await throwPrintError(response);
        }

        const data = await response.json();

        showNotification('Added to print queue', 'success');
        console.log('Print result:', data);
        if (typeof refreshJobs === 'function') refreshJobs();
    } catch (error) {
        console.error('Error printing text + image:', error);
        showNotification(`Error printing text + image: ${error.message}`, 'error');
    }
}

/**
 * Handle save settings form submission
 * @param {Event} event - Form submit event
 */
async function handleSaveSettings(event) {
    event.preventDefault();
    
    try {
        const printerUri = document.getElementById('printer-uri').value;
        const printerModel = document.getElementById('printer-model').value;
        const labelSize = document.getElementById('label-size').value;
        const fontSize = document.getElementById('text-font-size').value;
        const alignment = document.getElementById('text-alignment').value;
        const rotate = document.getElementById('rotate').value;
        const threshold = document.getElementById('threshold').value;
        const dither = document.getElementById('dither').value === 'true';
        const red = document.getElementById('red').value === 'true';
        const copies = parseInt(document.getElementById('copies').value) || 1;
        const cutMode = document.getElementById('cut-mode').value;
        const dpi600 = document.getElementById('dpi-600').value === 'true';
        const keepAliveEnabled = document.getElementById('keep-alive-enabled').value === 'true';
        const keepAliveInterval = parseInt(document.getElementById('keep-alive-interval').value);
        const keepAliveMode = document.getElementById('keep-alive-mode').value;
        const keepAliveDurationVal = parseInt(document.getElementById('keep-alive-duration-value').value) || 0;
        const keepAliveDurationUnit = document.getElementById('keep-alive-duration-unit').value;
        const keepAliveDurationSeconds = keepAliveDurationUnit === 'hours'
            ? keepAliveDurationVal * 3600
            : keepAliveDurationVal * 60;

        if (!printerUri || !printerModel || !labelSize) {
            throw new Error('Printer URI, model, and label size are required');
        }
        
        if (keepAliveInterval < 10) {
            throw new Error('Keep alive interval must be at least 10 seconds');
        }
        
        // Show loading state
        const submitBtn = event.target.querySelector('button[type="submit"]');
        const originalBtnText = submitBtn.innerHTML;
        submitBtn.disabled = true;
        submitBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Saving...';
        
        const response = await fetch('/api/v1/settings', {
            method: 'PUT',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                printer_uri: printerUri,
                printer_model: printerModel,
                label_size: labelSize,
                font_size: parseInt(fontSize),
                alignment: alignment,
                rotate: parseInt(rotate),
                threshold: parseFloat(threshold),
                dither: dither,
                red: red,
                copies: copies,
                cut_mode: cutMode,
                dpi_600: dpi600,
                keep_alive_enabled: keepAliveEnabled,
                keep_alive_interval: keepAliveInterval,
                keep_alive_mode: keepAliveMode,
                keep_alive_duration_seconds: keepAliveDurationSeconds
            })
        });
        
        // Reset button state
        submitBtn.disabled = false;
        submitBtn.innerHTML = originalBtnText;
        
        if (!response.ok) {
            const errorData = await response.json();
            throw new Error(errorData.message || `Error: ${response.status}`);
        }
        
        const data = await response.json();
        
        showNotification('Settings saved successfully', 'success');
        console.log('Settings saved:', data);
        
        // Update keep alive status based on new settings
        await updateKeepAlive(keepAliveEnabled, keepAliveInterval);
    } catch (error) {
        console.error('Error saving settings:', error);
        showNotification(`Error saving settings: ${error.message}`, 'error');
    }
}

// ===================== Hybrid live server preview =====================
//
// The client-side preview (preview.js) updates instantly while typing. In
// addition, we debounce a request to the server which renders the EXACT print
// label as a PNG and overlays it on top of the instant client preview.
//
// A single shared debounce timer + AbortController ensure only the latest
// request "wins": older in-flight requests are aborted and stale responses are
// ignored.

let serverPreviewTimer = null;
let serverPreviewController = null;

/**
 * Collect the shared printer/render settings exactly like the print handlers
 * do, so the server preview matches the real print output.
 */
function collectPreviewSettings() {
    return {
        printer_uri: document.getElementById('printer-uri').value,
        printer_model: document.getElementById('printer-model').value,
        label_size: document.getElementById('label-size').value,
        rotate: parseInt(document.getElementById('rotate').value),
        threshold: parseFloat(document.getElementById('threshold').value),
        dither: document.getElementById('dither').value === 'true',
        red: document.getElementById('red').value === 'true',
        copies: parseInt(document.getElementById('copies').value) || 1,
        cut_mode: document.getElementById('cut-mode').value,
        dpi_600: document.getElementById('dpi-600').value === 'true'
    };
}

/**
 * Hide the server preview image and clear its source. The client preview /
 * placeholder underneath then becomes visible again.
 */
function clearServerPreview() {
    const serverImg = document.getElementById('preview-server');
    if (serverImg) {
        serverImg.classList.add('d-none');
        // Use removeAttribute rather than src='' — an empty src makes the
        // browser try to load the page URL and logs a spurious ERR_INVALID_URL.
        serverImg.removeAttribute('src');
    }
}

/**
 * Show the server preview image and hide the client previews + placeholder so
 * the server-rendered label "wins".
 * @param {string} dataUrl - data:image/png;base64,... returned by the API
 */
function showServerPreview(dataUrl) {
    const serverImg = document.getElementById('preview-server');
    if (!serverImg) return;
    serverImg.src = dataUrl;
    serverImg.classList.remove('d-none');

    // Hide the instant client previews + placeholder; the server image wins.
    ['preview-text', 'preview-image', 'preview-qrcode', 'preview-label',
     'pdf-preview', 'preview-placeholder'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.classList.add('d-none');
    });
}

/**
 * Build the request descriptor (endpoint + body) for the given compose mode,
 * or return null when there is nothing to render (empty input / no file).
 * @param {string} mode - 'text' | 'qrcode' | 'label' | 'image'
 */
function buildPreviewRequest(mode) {
    const settings = collectPreviewSettings();

    if (mode === 'text') {
        const text = document.getElementById('text-input').value;
        if (!text.trim()) return null;
        return {
            url: '/api/v1/text/preview',
            json: {
                text: text,
                settings: Object.assign({}, settings, {
                    font_size: parseInt(document.getElementById('text-font-size').value),
                    alignment: document.getElementById('text-alignment').value
                })
            }
        };
    }

    if (mode === 'qrcode') {
        const qrData = document.getElementById('qr-data').value;
        if (!qrData.trim()) return null;
        const body = {
            qr: {
                data: qrData,
                size: parseInt(document.getElementById('qr-size').value),
                error_correction: document.getElementById('qr-error-correction').value,
                version: 1,
                box_size: 10,
                border: 4
            },
            settings: settings
        };
        const showText = document.getElementById('qr-show-text').checked;
        const textContent = document.getElementById('qr-text-content').value;
        if (showText && textContent) {
            body.text = {
                content: textContent,
                position: document.getElementById('qr-text-position').value,
                font_size: parseInt(document.getElementById('qr-text-font-size').value),
                alignment: document.getElementById('qr-text-alignment').value
            };
        }
        return { url: '/api/v1/qrcode/preview', json: body };
    }

    if (mode === 'label') {
        const qrData = document.getElementById('label-qr-data').value;
        const textContent = document.getElementById('label-text-content').value;
        if (!qrData.trim() || !textContent.trim()) return null;
        return {
            url: '/api/v1/label/preview',
            json: {
                qr: {
                    data: qrData,
                    position: document.getElementById('label-qr-position').value,
                    size: 400,
                    error_correction: document.getElementById('label-qr-error-correction').value || 'M',
                    version: 1,
                    box_size: 10,
                    border: 4
                },
                text: {
                    content: textContent,
                    font_size: parseInt(document.getElementById('label-text-font-size').value),
                    alignment: document.getElementById('label-text-alignment').value
                },
                settings: settings
            }
        };
    }

    if (mode === 'image') {
        const imageInput = document.getElementById('image-input');
        if (!imageInput || !imageInput.files || imageInput.files.length === 0) {
            return null;
        }
        const imageMode = document.getElementById('image-mode');
        let dither = settings.dither;
        if (imageMode.value === 'bw-dither') {
            dither = true;
        } else if (imageMode.value === 'bw') {
            dither = false;
        }
        const formData = new FormData();
        formData.append('image', imageInput.files[0]);
        formData.append('settings', JSON.stringify(Object.assign({}, settings, {
            dither: dither,
            image_mode: imageMode.value
        })));
        return { url: '/api/v1/image/preview', form: formData };
    }

    return null;
}

/**
 * Request a server-rendered, true-to-print preview for the given compose mode.
 * Debounced (~250ms) and abortable: the newest request always wins. On success
 * the returned PNG overlays the instant client preview; on any error / empty
 * input the server image is hidden so the client preview stays visible.
 * @param {string} mode - 'text' | 'qrcode' | 'label' | 'image'
 */
function requestServerPreview(mode) {
    clearTimeout(serverPreviewTimer);
    serverPreviewTimer = setTimeout(() => {
        const request = buildPreviewRequest(mode);

        // Nothing to render -> drop any server image, let the client preview show.
        if (!request) {
            clearServerPreview();
            return;
        }

        // Abort any in-flight request so its (older) response is ignored.
        if (serverPreviewController) {
            serverPreviewController.abort();
        }
        const controller = new AbortController();
        serverPreviewController = controller;

        const options = { method: 'POST', signal: controller.signal };
        if (request.form) {
            options.body = request.form;
        } else {
            options.headers = { 'Content-Type': 'application/json' };
            options.body = JSON.stringify(request.json);
        }

        fetch(request.url, options)
            .then(response => {
                // A newer request started meanwhile: ignore this result.
                if (serverPreviewController !== controller) return null;
                if (!response.ok) {
                    // 400 / invalid input -> keep the client preview, no server image.
                    clearServerPreview();
                    return null;
                }
                return response.json();
            })
            .then(data => {
                if (!data) return;
                if (serverPreviewController !== controller) return;
                if (data.image) {
                    showServerPreview(data.image);
                } else {
                    clearServerPreview();
                }
            })
            .catch(error => {
                // Swallow aborts from superseding requests.
                if (error && error.name === 'AbortError') return;
                console.error('Error generating server preview:', error);
                clearServerPreview();
            })
            .finally(() => {
                if (serverPreviewController === controller) {
                    serverPreviewController = null;
                }
            });
    }, 250);
}

/**
 * Update keep alive settings
 * @param {boolean} enabled - Whether keep alive should be enabled
 * @param {number} interval - Interval between pings in seconds
 */
async function updateKeepAlive(enabled, interval) {
    try {
        const response = await fetch('/api/v1/printers/keep-alive', {
            method: 'PUT',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                enabled: enabled,
                interval: interval
            })
        });
        
        if (!response.ok) {
            const errorData = await response.json();
            throw new Error(errorData.message || `Error: ${response.status}`);
        }
        
        const data = await response.json();
        
        // Update the status indicator
        loadKeepAliveStatus();
        
        console.log('Keep alive updated:', data);
    } catch (error) {
        console.error('Error updating keep alive:', error);
        showNotification(`Error updating keep alive: ${error.message}`, 'error');
    }
}

// ===================== Print queue =====================
//
// The print endpoints queue jobs that are processed asynchronously. The Queue
// panel lists those jobs, polled while it is the active tab. refreshJobs() does
// a single GET + render; startJobsPolling()/stopJobsPolling() (in core.js)
// control the interval.

const JOB_STATUS_META = {
    queued:    { label: 'Queued',    cls: 'queued' },
    printing:  { label: 'Printing',  cls: 'printing' },
    done:      { label: 'Done',      cls: 'done' },
    failed:    { label: 'Failed',    cls: 'failed' },
    cancelled: { label: 'Cancelled', cls: 'cancelled' }
};

/**
 * Escape a string for safe insertion into innerHTML.
 */
function escapeHtml(value) {
    if (value == null) return '';
    return String(value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

/**
 * Format a timestamp into a compact relative ("12s ago") string with the
 * absolute time as a title. Accepts ISO strings or epoch seconds/ms.
 */
function formatJobTime(value) {
    if (!value) return { text: '', title: '' };
    let date;
    if (typeof value === 'number') {
        date = new Date(value < 1e12 ? value * 1000 : value);
    } else {
        date = new Date(value);
    }
    if (isNaN(date.getTime())) {
        return { text: String(value), title: String(value) };
    }
    const diffMs = Date.now() - date.getTime();
    const sec = Math.round(diffMs / 1000);
    let text;
    if (sec < 5) {
        text = 'just now';
    } else if (sec < 60) {
        text = `${sec}s ago`;
    } else if (sec < 3600) {
        text = `${Math.floor(sec / 60)}m ago`;
    } else if (sec < 86400) {
        text = `${Math.floor(sec / 3600)}h ago`;
    } else {
        text = `${Math.floor(sec / 86400)}d ago`;
    }
    return { text, title: date.toLocaleString() };
}

/**
 * Update the sidebar badge with the number of active (queued + printing) jobs.
 */
function updateQueueBadge(jobs) {
    const badge = document.getElementById('queue-badge');
    if (!badge) return;
    const active = jobs.filter(j => j.status === 'queued' || j.status === 'printing').length;
    if (active > 0) {
        badge.textContent = String(active);
        badge.hidden = false;
    } else {
        badge.hidden = true;
    }
}

// Cache of the most recently rendered jobs, keyed by id, so per-row actions
// (e.g. Open) can read the job's `params` without an extra round-trip.
const jobsById = {};

/**
 * Render the list of jobs into the Queue panel.
 */
function renderJobs(jobs) {
    const list = document.getElementById('queue-list');
    if (!list) return;

    // Refresh the id -> job cache for action handlers.
    for (const key in jobsById) delete jobsById[key];
    if (Array.isArray(jobs)) {
        jobs.forEach(job => { if (job && job.id != null) jobsById[job.id] = job; });
    }

    if (!Array.isArray(jobs) || jobs.length === 0) {
        list.innerHTML =
            '<div class="queue-empty">' +
            '<i class="bi bi-inbox"></i>' +
            '<p>No print jobs yet</p>' +
            '</div>';
        return;
    }

    const rows = jobs.map(job => {
        const meta = JOB_STATUS_META[job.status] || { label: job.status || 'unknown', cls: 'queued' };
        const time = formatJobTime(job.finished_at || job.started_at || job.created_at);
        const spinner = job.status === 'printing'
            ? '<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span> '
            : '';
        const cancelBtn = job.status === 'queued'
            ? `<button type="button" class="btn-ghost btn-sm queue-cancel" data-action="cancel" data-job-id="${escapeHtml(job.id)}"><i class="bi bi-x-lg"></i> Cancel</button>`
            : '';
        const reprintBtns = job.can_reprint === true
            ? `<button type="button" class="btn-ghost btn-sm queue-reprint" data-action="reprint" data-job-id="${escapeHtml(job.id)}"><i class="bi bi-arrow-clockwise"></i> Reprint</button>` +
              `<button type="button" class="btn-ghost btn-sm queue-open" data-action="open" data-job-id="${escapeHtml(job.id)}"><i class="bi bi-box-arrow-up-right"></i> Open</button>`
            : '';
        // Delete is available for any job that is not currently printing.
        const deleteBtn = job.status !== 'printing'
            ? `<button type="button" class="btn-ghost btn-sm queue-delete" data-action="delete" data-job-id="${escapeHtml(job.id)}" data-job-status="${escapeHtml(job.status || '')}" title="Delete job"><i class="bi bi-trash3"></i></button>`
            : '';
        const errorRow = (job.status === 'failed' && job.error)
            ? `<div class="queue-error">${escapeHtml(job.error)}</div>`
            : '';

        return (
            `<div class="queue-item">` +
                `<div class="queue-item-main">` +
                    `<span class="queue-status ${meta.cls}">${spinner}${escapeHtml(meta.label)}</span>` +
                    `<span class="queue-type">${escapeHtml(job.type || '')}</span>` +
                    `<span class="queue-label" title="${escapeHtml(job.label || '')}">${escapeHtml(job.label || '—')}</span>` +
                    `<span class="queue-time" title="${escapeHtml(time.title)}">${escapeHtml(time.text)}</span>` +
                    `<span class="queue-actions">${reprintBtns}${cancelBtn}${deleteBtn}</span>` +
                `</div>` +
                errorRow +
            `</div>`
        );
    });

    list.innerHTML = rows.join('');
}

/**
 * Fetch the current jobs and render them. Also refreshes the sidebar badge.
 */
async function refreshJobs() {
    try {
        const response = await fetch('/api/v1/jobs');
        if (!response.ok) {
            throw new Error(`Failed to load jobs: ${response.status}`);
        }
        const data = await response.json();
        const jobs = Array.isArray(data.jobs) ? data.jobs : [];
        renderJobs(jobs);
        updateQueueBadge(jobs);
    } catch (error) {
        console.error('Error loading jobs:', error);
    }
    // Keep the queue control state (pause/resume + paused badge) in sync,
    // folded into the same poll so we don't add a second fast interval.
    refreshQueueState();
}

/**
 * Reflect the queue control state (paused/running) in the header controls: the
 * Pause/Resume toggle's label + icon and the "Queue paused" badge.
 * @param {{paused?: boolean}} state
 */
function applyQueueState(state) {
    const paused = !!(state && state.paused);

    const toggle = document.getElementById('queue-pause-toggle');
    if (toggle) {
        toggle.innerHTML = paused
            ? '<i class="bi bi-play-fill"></i> Resume'
            : '<i class="bi bi-pause-fill"></i> Pause';
        toggle.setAttribute('aria-pressed', paused ? 'true' : 'false');
        toggle.title = paused ? 'Resume the queue' : 'Pause the queue';
    }

    const badge = document.getElementById('queue-paused-badge');
    if (badge) badge.hidden = !paused;
}

/**
 * Fetch the current queue control state (paused/queued/printing counts) and
 * reflect it in the header controls. Folded into the polling loop alongside
 * refreshJobs so it stays in sync without a second fast interval.
 */
async function refreshQueueState() {
    try {
        const response = await fetch('/api/v1/jobs/queue');
        if (!response.ok) {
            throw new Error(`Failed to load queue state: ${response.status}`);
        }
        const state = await response.json();
        applyQueueState(state);
    } catch (error) {
        console.error('Error loading queue state:', error);
    }
}

/**
 * Toggle the queue between paused and running, then refresh state + list.
 * Reads the current state from the toggle button's aria-pressed flag.
 */
async function toggleQueuePause() {
    const toggle = document.getElementById('queue-pause-toggle');
    const paused = toggle && toggle.getAttribute('aria-pressed') === 'true';
    const endpoint = paused ? 'resume' : 'pause';
    try {
        const response = await fetch(`/api/v1/jobs/${endpoint}`, { method: 'POST' });
        if (!response.ok) {
            throw new Error(`Error: ${response.status}`);
        }
        const state = await response.json();
        applyQueueState(state);
        showNotification(paused ? 'Queue resumed' : 'Queue paused', 'success');
    } catch (error) {
        console.error('Error toggling queue:', error);
        showNotification(`Error toggling queue: ${error.message}`, 'error');
        refreshQueueState();
    } finally {
        refreshJobs();
    }
}

/**
 * Stop the queue: pause it and cancel all waiting jobs. Confirms first, then
 * surfaces how many jobs were cancelled.
 */
async function stopQueue() {
    const confirmed = await confirmDialog(
        'Stop the queue and cancel all waiting jobs?',
        { title: 'Stop queue', confirmLabel: 'Stop' }
    );
    if (!confirmed) return;

    try {
        const response = await fetch('/api/v1/jobs/stop', { method: 'POST' });
        if (!response.ok) {
            throw new Error(`Error: ${response.status}`);
        }
        const data = await response.json();
        applyQueueState(data);
        const n = data.cancelled || 0;
        showNotification(`Stopped — ${n} job${n === 1 ? '' : 's'} cancelled`, 'success');
    } catch (error) {
        console.error('Error stopping queue:', error);
        showNotification(`Error stopping queue: ${error.message}`, 'error');
        refreshQueueState();
    } finally {
        refreshJobs();
    }
}

/**
 * Clear ALL jobs (including waiting ones): cancels every queued job and removes
 * all jobs except one that may currently be printing. Confirms first.
 */
async function clearAllJobs() {
    const confirmed = await confirmDialog(
        'Delete ALL jobs, including waiting ones?',
        { title: 'Clear all', confirmLabel: 'Delete all' }
    );
    if (!confirmed) return;

    try {
        const response = await fetch('/api/v1/jobs/clear-all', { method: 'POST' });
        if (!response.ok) {
            throw new Error(`Error: ${response.status}`);
        }
        const data = await response.json();
        const n = data.cleared || 0;
        showNotification(`Cleared ${n} job${n === 1 ? '' : 's'}`, 'success');
    } catch (error) {
        console.error('Error clearing all jobs:', error);
        showNotification(`Error clearing all jobs: ${error.message}`, 'error');
    } finally {
        refreshJobs();
    }
}

/**
 * Delete a single job (queued OR finished). A queued job is confirmed first; an
 * already-finished job is deleted without a prompt to keep it quick. A printing
 * job cannot be deleted (the server returns removed:false).
 * @param {string} jobId
 * @param {string} status - the job's current status (for the confirm decision)
 */
async function deleteJob(jobId, status) {
    if (status === 'queued') {
        const confirmed = await confirmDialog('Delete this waiting job?', {
            title: 'Delete job',
            confirmLabel: 'Delete'
        });
        if (!confirmed) return;
    }

    try {
        const response = await fetch(`/api/v1/jobs/${encodeURIComponent(jobId)}/delete`, {
            method: 'POST'
        });
        if (!response.ok) {
            throw new Error(`Error: ${response.status}`);
        }
        const data = await response.json();
        if (!data.removed) {
            showNotification('Job could not be deleted', 'warning');
        }
    } catch (error) {
        console.error('Error deleting job:', error);
        showNotification(`Error deleting job: ${error.message}`, 'error');
    } finally {
        refreshJobs();
    }
}

/**
 * Cancel a queued job, then refresh the list.
 * @param {string} jobId
 */
async function cancelJob(jobId) {
    try {
        const response = await fetch(`/api/v1/jobs/${encodeURIComponent(jobId)}/cancel`, {
            method: 'POST'
        });
        if (!response.ok) {
            throw new Error(`Error: ${response.status}`);
        }
        const data = await response.json();
        if (data.cancelled) {
            showNotification('Job cancelled', 'success');
        } else {
            showNotification('Job could not be cancelled', 'warning');
        }
    } catch (error) {
        console.error('Error cancelling job:', error);
        showNotification(`Error cancelling job: ${error.message}`, 'error');
    } finally {
        refreshJobs();
    }
}

/**
 * Show a Bootstrap confirmation dialog and resolve to true/false based on the
 * user's choice. Falls back to a native confirm() if Bootstrap or the modal
 * markup is unavailable.
 * @param {string} message - The question shown to the user.
 * @param {Object} [options]
 * @param {string} [options.title] - Modal title.
 * @param {string} [options.confirmLabel] - Confirm button label.
 * @returns {Promise<boolean>}
 */
function confirmDialog(message, options = {}) {
    const modalEl = document.getElementById('confirmModal');
    if (!modalEl || !(window.bootstrap && bootstrap.Modal)) {
        return Promise.resolve(window.confirm(message));
    }

    return new Promise(resolve => {
        const messageEl = document.getElementById('confirm-message');
        const okBtn = document.getElementById('confirm-ok');
        const titleEl = document.getElementById('confirmModalLabel');

        if (messageEl) messageEl.textContent = message;
        if (titleEl) titleEl.textContent = options.title || 'Confirm';
        if (okBtn) okBtn.textContent = options.confirmLabel || 'Confirm';

        const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
        let confirmed = false;

        const onOk = () => {
            confirmed = true;
            modal.hide();
        };
        const onHidden = () => {
            if (okBtn) okBtn.removeEventListener('click', onOk);
            modalEl.removeEventListener('hidden.bs.modal', onHidden);
            resolve(confirmed);
        };

        if (okBtn) okBtn.addEventListener('click', onOk);
        modalEl.addEventListener('hidden.bs.modal', onHidden);
        modal.show();
    });
}

/**
 * Re-queue a previous job for printing via its persisted params, then refresh.
 * Asks the user to confirm before re-queuing.
 * @param {string} jobId
 */
async function reprintJob(jobId) {
    const confirmed = await confirmDialog('Really reprint this job?', {
        title: 'Reprint Job',
        confirmLabel: 'Reprint'
    });
    if (!confirmed) return;

    try {
        const response = await fetch(`/api/v1/jobs/${encodeURIComponent(jobId)}/reprint`, {
            method: 'POST'
        });
        if (!response.ok) {
            let message = `Error: ${response.status}`;
            try {
                const errorData = await response.json();
                message = errorData.message || message;
            } catch (e) { /* non-JSON body */ }
            throw new Error(message);
        }
        showNotification('Re-queued for printing', 'success');
    } catch (error) {
        console.error('Error re-printing job:', error);
        showNotification(`Error re-printing job: ${error.message}`, 'error');
    } finally {
        refreshJobs();
    }
}

/**
 * Helper: set a form field's value if the element exists.
 */
function setFieldValue(id, value) {
    const el = document.getElementById(id);
    if (el && value != null) el.value = value;
}

/**
 * Helper: set a select rendered as a boolean ('true'/'false') if the element
 * exists and the value is a boolean.
 */
function setBoolField(id, value) {
    if (typeof value !== 'boolean') return;
    const el = document.getElementById(id);
    if (el) el.value = value ? 'true' : 'false';
}

/**
 * Populate the shared printer/render settings fields from a settings object.
 * Robust against missing fields and missing settings.
 */
function applySettingsToForm(settings) {
    if (!settings || typeof settings !== 'object') return;
    setFieldValue('printer-uri', settings.printer_uri);
    setFieldValue('printer-model', settings.printer_model);
    setFieldValue('label-size', settings.label_size);
    setFieldValue('rotate', settings.rotate != null ? String(settings.rotate) : null);
    setFieldValue('threshold', settings.threshold != null ? String(settings.threshold) : null);
    setBoolField('dither', settings.dither);
    setBoolField('red', settings.red);
    setFieldValue('copies', settings.copies != null ? String(settings.copies) : null);
    setFieldValue('cut-mode', settings.cut_mode);
    setBoolField('dpi-600', settings.dpi_600);
}

/**
 * Activate a compose tab by its trigger button id (Bootstrap Tab + click
 * fallback) and close the mobile drawer if present.
 */
function activateComposeTab(tabId) {
    const tabBtn = document.getElementById(tabId);
    if (tabBtn) {
        if (window.bootstrap && bootstrap.Tab) {
            new bootstrap.Tab(tabBtn).show();
        } else {
            tabBtn.click();
        }
    }
    // Close the off-canvas drawer on mobile.
    const rail = document.getElementById('rail');
    if (rail && rail.classList.contains('open') &&
        window.matchMedia('(max-width: 768px)').matches) {
        rail.classList.remove('open');
        const railScrim = document.getElementById('rail-scrim');
        if (railScrim) railScrim.hidden = true;
        const railToggle = document.getElementById('rail-toggle');
        if (railToggle) railToggle.setAttribute('aria-expanded', 'false');
    }
}

/**
 * Helper: dispatch an event on a field if it exists.
 */
function dispatchOn(id, eventName) {
    const el = document.getElementById(id);
    if (el) el.dispatchEvent(new Event(eventName, { bubbles: true }));
}

/**
 * Load a persisted job's params back into the matching compose form and switch
 * to its tab so the user only needs to press "Print". For image/pdf jobs the
 * persisted file is fetched from the server.
 * @param {string} jobId
 */
async function openJob(jobId) {
    let job = jobsById[jobId];

    // Fall back to a fresh fetch if the job (or its params) is not cached.
    if (!job || !job.params) {
        try {
            const response = await fetch(`/api/v1/jobs/${encodeURIComponent(jobId)}`);
            if (response.ok) {
                job = await response.json();
            }
        } catch (e) {
            console.error('Error loading job:', e);
        }
    }

    if (!job || !job.params) {
        showNotification('Job details are no longer available', 'error');
        return;
    }

    const params = job.params;
    const type = params.type;
    const settings = params.settings || {};

    try {
        if (type === 'text') {
            applySettingsToForm(settings);
            setFieldValue('text-input', params.text);
            setFieldValue('text-font-size', settings.font_size != null ? String(settings.font_size) : null);
            setFieldValue('text-alignment', settings.alignment);
            activateComposeTab('text-tab');
            dispatchOn('text-input', 'input');
        } else if (type === 'qrcode') {
            applySettingsToForm(settings);
            setFieldValue('qr-data', params.data);
            setFieldValue('qr-size', settings.size != null ? String(settings.size) : null);
            setFieldValue('qr-error-correction', settings.error_correction);
            activateComposeTab('qrcode-tab');
            dispatchOn('qr-data', 'input');
        } else if (type === 'label') {
            applySettingsToForm(settings);
            setFieldValue('label-text-content', params.text);
            setFieldValue('label-qr-data', params.data);
            setFieldValue('label-text-font-size', settings.font_size != null ? String(settings.font_size) : null);
            setFieldValue('label-text-alignment', settings.alignment);
            setFieldValue('label-qr-position', settings.qr_position);
            setFieldValue('label-qr-error-correction', settings.error_correction);
            activateComposeTab('label-tab');
            dispatchOn('label-text-content', 'input');
        } else if (type === 'image') {
            applySettingsToForm(settings);
            setFieldValue('image-mode', settings.image_mode);
            const loaded = await loadJobFileIntoInput(jobId, 'image-input', params.filename || 'reprint-image');
            if (!loaded) return;
            activateComposeTab('image-tab');
            dispatchOn('image-input', 'change');
        } else if (type === 'pdf') {
            applySettingsToForm(settings);
            setFieldValue('pdf-pages', params.pages);
            setFieldValue('pdf-scale-mode', params.scale_mode);
            const loaded = await loadJobFileIntoInput(jobId, 'pdf-input', params.filename || 'reprint.pdf');
            if (!loaded) return;
            activateComposeTab('pdf-tab');
            dispatchOn('pdf-input', 'change');
        } else {
            showNotification('Unsupported job type', 'error');
            return;
        }
    } catch (error) {
        console.error('Error opening job:', error);
        showNotification(`Error opening job: ${error.message}`, 'error');
    }
}

/**
 * Fetch a job's persisted file and place it into the given file input via a
 * DataTransfer. Returns true on success, false on failure (e.g. expired file).
 * @param {string} jobId
 * @param {string} inputId
 * @param {string} fileName
 */
async function loadJobFileIntoInput(jobId, inputId, fileName) {
    const input = document.getElementById(inputId);
    if (!input) return false;

    let response;
    try {
        response = await fetch(`/api/v1/jobs/${encodeURIComponent(jobId)}/file`);
    } catch (e) {
        showNotification('File no longer available (expired)', 'error');
        return false;
    }

    if (response.status === 404) {
        showNotification('File no longer available (expired)', 'error');
        return false;
    }
    if (!response.ok) {
        showNotification('File no longer available (expired)', 'error');
        return false;
    }

    const blob = await response.blob();
    const file = new File([blob], fileName, { type: blob.type });
    const dt = new DataTransfer();
    dt.items.add(file);
    input.files = dt.files;
    return true;
}

/**
 * Clear all finished jobs (done/failed/cancelled) from the queue.
 */
async function clearFinishedJobs() {
    try {
        const response = await fetch('/api/v1/jobs/clear', { method: 'POST' });
        if (!response.ok) {
            throw new Error(`Error: ${response.status}`);
        }
        const data = await response.json();
        const n = data.cleared || 0;
        showNotification(`Cleared ${n} finished job${n === 1 ? '' : 's'}`, 'success');
    } catch (error) {
        console.error('Error clearing jobs:', error);
        showNotification(`Error clearing jobs: ${error.message}`, 'error');
    } finally {
        refreshJobs();
    }
}
