// Brother QL Printer App - API Interactions

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
        document.getElementById('copies').value = settings.copies || 1;
        document.getElementById('cut-mode').value = settings.cut_mode || 'each';
        document.getElementById('dpi-600').value = settings.dpi_600 ? 'true' : 'false';
        document.getElementById('keep-alive-enabled').value = settings.keep_alive_enabled ? 'true' : 'false';
        document.getElementById('keep-alive-interval').value = settings.keep_alive_interval || '60';
        
        // Also check the current keep alive status
        loadKeepAliveStatus();
        
        console.log('Settings loaded successfully');
    } catch (error) {
        console.error('Error loading settings:', error);
        showNotification('Error loading settings', 'error');
    }
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
        
        // Show loading state
        const submitBtn = event.target.querySelector('button[type="submit"]');
        const originalBtnText = submitBtn.innerHTML;
        submitBtn.disabled = true;
        submitBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Printing...';
        
        const response = await fetch('/api/v1/text/print', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
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
                    copies: parseInt(document.getElementById('copies').value) || 1,
                    cut_mode: document.getElementById('cut-mode').value,
                    dpi_600: document.getElementById('dpi-600').value === 'true'
                }
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

        showNotification('Text printed successfully', 'success');
        console.log('Print result:', data);
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
            copies: parseInt(document.getElementById('copies').value) || 1,
            cut_mode: document.getElementById('cut-mode').value,
            dpi_600: document.getElementById('dpi-600').value === 'true',
            image_mode: imageMode.value
        }));
        
        const response = await fetch('/api/v1/image/print', {
            method: 'POST',
            body: formData
        });
        
        // Reset button state
        submitBtn.disabled = false;
        submitBtn.innerHTML = originalBtnText;
        
        if (!response.ok) {
            const errorData = await response.json();
            throw new Error(errorData.message || `Error: ${response.status}`);
        }
        
        const data = await response.json();
        
        showNotification('Image printed successfully', 'success');
        console.log('Print result:', data);
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
            copies: parseInt(document.getElementById('copies').value) || 1,
            cut_mode: document.getElementById('cut-mode').value,
            dpi_600: document.getElementById('dpi-600').value === 'true'
        }));
        formData.append('pages', pages);
        formData.append('scale_mode', scaleMode);

        const response = await fetch('/api/v1/pdf/print', {
            method: 'POST',
            body: formData
        });

        // Reset button state
        submitBtn.disabled = false;
        submitBtn.innerHTML = originalBtnText;

        if (!response.ok) {
            const errorData = await response.json();
            throw new Error(errorData.message || `Error: ${response.status}`);
        }

        const data = await response.json();

        showNotification('PDF printed successfully', 'success');
        console.log('Print result:', data);
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
                copies: parseInt(document.getElementById('copies').value) || 1,
                cut_mode: document.getElementById('cut-mode').value,
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
            const errorData = await response.json();
            throw new Error(errorData.message || `Error: ${response.status}`);
        }
        
        const data = await response.json();
        
        showNotification('QR code printed successfully', 'success');
        console.log('Print result:', data);
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
                copies: parseInt(document.getElementById('copies').value) || 1,
                cut_mode: document.getElementById('cut-mode').value,
                dpi_600: document.getElementById('dpi-600').value === 'true'
            }
        };

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
            const errorData = await response.json();
            throw new Error(errorData.message || `Error: ${response.status}`);
        }
        
        const data = await response.json();
        
        showNotification('Label printed successfully', 'success');
        console.log('Print result:', data);
    } catch (error) {
        console.error('Error printing label:', error);
        showNotification(`Error printing label: ${error.message}`, 'error');
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
                keep_alive_interval: keepAliveInterval
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
        serverImg.src = '';
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
