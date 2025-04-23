// Brother QL Printer App - Preview Functionality

/**
 * Initialize preview elements
 */
function initPreviewElements() {
    // Get preview elements
    const previewText = document.getElementById('preview-text');
    const previewImage = document.getElementById('preview-image');
    const previewQrcode = document.getElementById('preview-qrcode');
    const previewLabel = document.getElementById('preview-label');
    const previewPlaceholder = document.getElementById('preview-placeholder');
    
    // Make sure all preview elements exist
    if (previewText) previewText.classList.add('d-none');
    if (previewImage) previewImage.classList.add('d-none');
    if (previewQrcode) previewQrcode.classList.add('d-none');
    if (previewLabel) previewLabel.classList.add('d-none');
    if (previewPlaceholder) previewPlaceholder.classList.remove('d-none');
}

/**
 * Initialize placeholders for QR code previews
 */
function initQRCodePlaceholders() {
    const previewQrcode = document.getElementById('preview-qrcode');
    const previewLabel = document.getElementById('preview-label');
    
    if (previewQrcode) {
        previewQrcode.classList.add('d-none');
    }
    
    if (previewLabel) {
        previewLabel.classList.add('d-none');
    }
}

/**
 * Check if all previews are empty/hidden
 */
function areAllPreviewsEmpty() {
    const previewText = document.getElementById('preview-text');
    const previewImage = document.getElementById('preview-image');
    const previewQrcode = document.getElementById('preview-qrcode');
    const previewLabel = document.getElementById('preview-label');
    
    return (
        (!previewText || previewText.classList.contains('d-none')) &&
        (!previewImage || previewImage.classList.contains('d-none')) &&
        (!previewQrcode || previewQrcode.classList.contains('d-none')) &&
        (!previewLabel || previewLabel.classList.contains('d-none'))
    );
}

/**
 * Hide all previews except the specified one
 */
function hideOtherPreviews(exceptId) {
    const allPreviews = ['preview-text', 'preview-image', 'preview-qrcode', 'preview-label'];
    const previewPlaceholder = document.getElementById('preview-placeholder');
    
    allPreviews.forEach(id => {
        if (id !== exceptId) {
            const element = document.getElementById(id);
            if (element) element.classList.add('d-none');
        }
    });
    
    // Hide placeholder when showing any preview
    if (previewPlaceholder) previewPlaceholder.classList.add('d-none');
}

/**
 * Update text preview
 */
function updateTextPreview() {
    const textInput = document.getElementById('text-input');
    const textFontSize = document.getElementById('text-font-size');
    const textAlignment = document.getElementById('text-alignment');
    const previewText = document.getElementById('preview-text');
    const previewPlaceholder = document.getElementById('preview-placeholder');
    
    if (textInput && textFontSize && textAlignment && previewText) {
        const text = textInput.value.trim();
        const fontSize = textFontSize.value;
        const alignment = textAlignment.value;
        
        // Show or hide elements based on content
        if (text) {
            // Format text with HTML
            const formattedText = text
                .replace(/\n/g, '<br>')
                .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
                .replace(/\*(.*?)\*/g, '<em>$1</em>');
            
            // Update preview
            previewText.innerHTML = formattedText;
            previewText.style.fontSize = `${fontSize}px`;
            previewText.style.textAlign = alignment;
            previewText.classList.remove('d-none');
            
            // Hide placeholder and other previews
            if (previewPlaceholder) previewPlaceholder.classList.add('d-none');
            hideOtherPreviews('preview-text');
        } else {
            // Hide text preview if empty
            previewText.classList.add('d-none');
            
            // Show placeholder if all previews are empty
            if (areAllPreviewsEmpty() && previewPlaceholder) {
                previewPlaceholder.classList.remove('d-none');
            }
        }
    }
}

/**
 * Update QR code preview
 */
function updateQRCodePreview() {
    const qrData = document.getElementById('qr-data');
    const qrSize = document.getElementById('qr-size');
    const qrErrorCorrection = document.getElementById('qr-error-correction');
    const qrShowText = document.getElementById('qr-show-text');
    const qrTextContent = document.getElementById('qr-text-content');
    const qrTextPosition = document.getElementById('qr-text-position');
    const qrTextFontSize = document.getElementById('qr-text-font-size');
    const qrTextAlignment = document.getElementById('qr-text-alignment');
    const previewQrcode = document.getElementById('preview-qrcode');
    const previewPlaceholder = document.getElementById('preview-placeholder');
    
    if (!qrData || !previewQrcode) return;
    
    const data = qrData.value.trim();
    
    // Clear previous preview
    previewQrcode.innerHTML = '';
    
    if (data && typeof qrcode === 'function') {
        try {
            // Get QR code settings
            const size = parseInt(qrSize.value) || 400;
            const errorCorrectionLevel = qrErrorCorrection.value || 'M';
            const showText = qrShowText && qrShowText.checked;
            const textContent = qrTextContent ? (qrTextContent.value || data) : data;
            const textPosition = qrTextPosition ? qrTextPosition.value || 'bottom' : 'bottom';
            const textFontSize = parseInt(qrTextFontSize ? qrTextFontSize.value : '30') || 30;
            const textAlignment = qrTextAlignment ? qrTextAlignment.value || 'center' : 'center';
            
            // Create QR code
            const typeNumber = 0; // Auto-detect
            const qr = qrcode(typeNumber, errorCorrectionLevel);
            qr.addData(data);
            qr.make();
            
            // Calculate size
            const cellSize = Math.floor(size / qr.getModuleCount());
            const margin = 4; // Border size
            
            // Create QR code image
            const qrImg = qr.createImgTag(cellSize, margin);
            
            // Create container for QR code
            const qrContainer = document.createElement('div');
            qrContainer.innerHTML = qrImg;
            qrContainer.style.textAlign = 'center';
            
            // Add text if needed
            if (showText && textContent) {
                const textElement = document.createElement('div');
                textElement.className = 'qr-text';
                textElement.textContent = textContent;
                textElement.style.fontSize = `${textFontSize}px`;
                textElement.style.textAlign = textAlignment || 'center';
                
                if (textPosition === 'top') {
                    previewQrcode.appendChild(textElement);
                    previewQrcode.appendChild(qrContainer);
                } else {
                    previewQrcode.appendChild(qrContainer);
                    previewQrcode.appendChild(textElement);
                }
            } else {
                previewQrcode.appendChild(qrContainer);
            }
            
            // Show QR code preview and hide others
            previewQrcode.classList.remove('d-none');
            hideOtherPreviews('preview-qrcode');
        } catch (error) {
            console.error('Error generating QR code:', error);
            previewQrcode.classList.add('d-none');
            
            // Show placeholder if all previews are empty
            if (areAllPreviewsEmpty() && previewPlaceholder) {
                previewPlaceholder.classList.remove('d-none');
            }
        }
    } else {
        // Hide QR code preview if no data
        previewQrcode.classList.add('d-none');
        
        // Show placeholder if all previews are empty
        if (areAllPreviewsEmpty() && previewPlaceholder) {
            previewPlaceholder.classList.remove('d-none');
        }
    }
}

/**
 * Update label preview
 */
function updateLabelPreview() {
    const labelQrData = document.getElementById('label-qr-data');
    const labelQrPosition = document.getElementById('label-qr-position');
    const labelQrErrorCorrection = document.getElementById('label-qr-error-correction');
    const labelTextContent = document.getElementById('label-text-content');
    const labelTextFontSize = document.getElementById('label-text-font-size');
    const labelTextAlignment = document.getElementById('label-text-alignment');
    const previewLabel = document.getElementById('preview-label');
    const previewPlaceholder = document.getElementById('preview-placeholder');
    
    if (!labelQrData || !labelTextContent || !previewLabel) return;
    
    const qrData = labelQrData.value.trim();
    const textContent = labelTextContent.value.trim();
    
    // Clear previous preview
    previewLabel.innerHTML = '';
    
    // Set class for QR position
    previewLabel.className = 'preview-label';
    if (labelQrPosition && labelQrPosition.value === 'left') {
        previewLabel.classList.add('qr-left');
    }
    
    if (qrData && textContent && typeof qrcode === 'function') {
        try {
            // Get label settings
            const qrPosition = labelQrPosition ? labelQrPosition.value || 'right' : 'right';
            const errorCorrectionLevel = labelQrErrorCorrection ? labelQrErrorCorrection.value || 'M' : 'M';
            const textFontSize = parseInt(labelTextFontSize ? labelTextFontSize.value : '30') || 30;
            const textAlignment = labelTextAlignment ? labelTextAlignment.value || 'left' : 'left';
            
            // Create text div
            const textDiv = document.createElement('div');
            textDiv.className = 'label-text';
            textDiv.style.fontSize = `${textFontSize}px`;
            textDiv.style.textAlign = textAlignment;
            // Format text with HTML to handle line breaks
            const formattedText = textContent
                .replace(/\n/g, '<br>')
                .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
                .replace(/\*(.*?)\*/g, '<em>$1</em>');
            
            textDiv.innerHTML = formattedText;
            
            // Create QR code div
            const qrDiv = document.createElement('div');
            qrDiv.className = 'label-qr';
            
            // Create QR code
            const typeNumber = 0; // Auto-detect
            const qr = qrcode(typeNumber, errorCorrectionLevel);
            qr.addData(qrData);
            qr.make();
            
            // Calculate size for QR code (1/3 of label width)
            const cellSize = Math.floor(200 / qr.getModuleCount());
            const margin = 4; // Border size
            
            // Create QR code image
            const qrImg = qr.createImgTag(cellSize, margin);
            qrDiv.innerHTML = qrImg;
            
            // Add elements to preview
            previewLabel.appendChild(textDiv);
            previewLabel.appendChild(qrDiv);
            
            // Show label preview and hide others
            previewLabel.classList.remove('d-none');
            hideOtherPreviews('preview-label');
        } catch (error) {
            console.error('Error generating label preview:', error);
            previewLabel.classList.add('d-none');
            
            // Show placeholder if all previews are empty
            if (areAllPreviewsEmpty() && previewPlaceholder) {
                previewPlaceholder.classList.remove('d-none');
            }
        }
    } else {
        // Hide label preview if data is missing
        previewLabel.classList.add('d-none');
        
        // Show placeholder if all previews are empty
        if (areAllPreviewsEmpty() && previewPlaceholder) {
            previewPlaceholder.classList.remove('d-none');
        }
    }
}

/**
 * Handle image preview when a file is selected
 * @param {Event} event - Change event
 */
function handleImagePreview(event) {
    const previewImage = document.getElementById('preview-image');
    const previewPlaceholder = document.getElementById('preview-placeholder');
    const imageMode = document.getElementById('image-mode');
    
    if (event.target.files && event.target.files[0]) {
        const reader = new FileReader();
        
        reader.onload = function(e) {
            // Store the original image data
            previewImage.dataset.originalSrc = e.target.result;
            
            // Apply the selected image mode
            applyImageMode(previewImage, imageMode.value);
            
            previewImage.classList.remove('d-none');
            
            // Hide placeholder and other previews
            if (previewPlaceholder) previewPlaceholder.classList.add('d-none');
            hideOtherPreviews('preview-image');
        };
        
        reader.readAsDataURL(event.target.files[0]);
        
        // Add event listener to image mode select if not already added
        if (!imageMode.dataset.listenerAdded) {
            imageMode.addEventListener('change', function() {
                if (previewImage.dataset.originalSrc) {
                    applyImageMode(previewImage, this.value);
                }
            });
            imageMode.dataset.listenerAdded = 'true';
        }
    } else {
        // Hide image preview
        previewImage.src = '';
        previewImage.dataset.originalSrc = '';
        previewImage.classList.add('d-none');
        
        // Show placeholder if all previews are empty
        if (areAllPreviewsEmpty() && previewPlaceholder) {
            previewPlaceholder.classList.remove('d-none');
        }
    }
}

/**
 * Apply image processing mode to the preview image
 * @param {HTMLImageElement} imageElement - The image element to process
 * @param {string} mode - The processing mode (color, bw, bw-dither)
 */
function applyImageMode(imageElement, mode) {
    if (!imageElement.dataset.originalSrc) return;
    
    if (mode === 'color') {
        // Use original image
        imageElement.src = imageElement.dataset.originalSrc;
        imageElement.style.filter = 'none';
        return;
    }
    
    // For black and white modes, we'll use a canvas to process the image
    const img = new Image();
    img.onload = function() {
        const canvas = document.createElement('canvas');
        const ctx = canvas.getContext('2d');
        
        canvas.width = img.width;
        canvas.height = img.height;
        
        // Draw the original image
        ctx.drawImage(img, 0, 0);
        
        // Get image data
        const imageData = ctx.getImageData(0, 0, canvas.width, canvas.height);
        const data = imageData.data;
        
        if (mode === 'bw') {
            // Simple black and white conversion (threshold at 128)
            for (let i = 0; i < data.length; i += 4) {
                const avg = (data[i] + data[i + 1] + data[i + 2]) / 3;
                const val = avg > 128 ? 255 : 0;
                data[i] = data[i + 1] = data[i + 2] = val;
            }
        } else if (mode === 'bw-dither') {
            // Floyd-Steinberg dithering
            const width = canvas.width;
            const height = canvas.height;
            
            // Convert to grayscale first
            for (let i = 0; i < data.length; i += 4) {
                const avg = (data[i] + data[i + 1] + data[i + 2]) / 3;
                data[i] = data[i + 1] = data[i + 2] = avg;
            }
            
            // Apply dithering
            for (let y = 0; y < height; y++) {
                for (let x = 0; x < width; x++) {
                    const idx = (y * width + x) * 4;
                    const oldPixel = data[idx];
                    const newPixel = oldPixel > 128 ? 255 : 0;
                    const error = oldPixel - newPixel;
                    
                    data[idx] = data[idx + 1] = data[idx + 2] = newPixel;
                    
                    // Distribute error to neighboring pixels
                    if (x + 1 < width) {
                        data[idx + 4] += error * 7 / 16;
                        data[idx + 5] += error * 7 / 16;
                        data[idx + 6] += error * 7 / 16;
                    }
                    
                    if (y + 1 < height) {
                        if (x > 0) {
                            data[idx + 4 * width - 4] += error * 3 / 16;
                            data[idx + 4 * width - 3] += error * 3 / 16;
                            data[idx + 4 * width - 2] += error * 3 / 16;
                        }
                        
                        data[idx + 4 * width] += error * 5 / 16;
                        data[idx + 4 * width + 1] += error * 5 / 16;
                        data[idx + 4 * width + 2] += error * 5 / 16;
                        
                        if (x + 1 < width) {
                            data[idx + 4 * width + 4] += error * 1 / 16;
                            data[idx + 4 * width + 5] += error * 1 / 16;
                            data[idx + 4 * width + 6] += error * 1 / 16;
                        }
                    }
                }
            }
        }
        
        // Put the processed image data back
        ctx.putImageData(imageData, 0, 0);
        
        // Update the image source
        imageElement.src = canvas.toDataURL('image/png');
    };
    
    img.src = imageElement.dataset.originalSrc;
}
