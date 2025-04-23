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
                    red: red
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
                red: red
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
                red: red
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
