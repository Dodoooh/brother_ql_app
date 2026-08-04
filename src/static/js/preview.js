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
 * Reflect the selected label medium in the preview panel.
 *
 * Round die-cut media (d12 / d24 / d58) is rendered by the printer as a square
 * raster, but the label is punched out as a circle: only the inscribed circle
 * ends up on the label. The server preview keeps showing the raster exactly as
 * printed — this only adds the die-cut treatment around it (cut edge + veiled
 * corners) and a hint naming the medium. Rectangular die-cut types ("62x29")
 * and continuous rolls stay rectangular.
 */
function updatePreviewMediumUI() {
    const labelSizeEl = document.getElementById('label-size');
    const stage = document.getElementById('preview-stage');
    const serverImg = document.getElementById('preview-server');
    const hint = document.getElementById('preview-medium-hint');
    if (!labelSizeEl) return;

    const diameter = typeof roundLabelDiameterMm === 'function'
        ? roundLabelDiameterMm(labelSizeEl.value)
        : null;
    const round = diameter !== null;

    if (stage) stage.classList.toggle('is-round', round);
    if (serverImg) {
        serverImg.alt = round
            ? 'Rendered label preview (round die-cut)'
            : 'Rendered label preview';
    }
    if (hint) {
        hint.textContent = round
            ? `${diameter} mm round die-cut — only the circle ends up on the label, the hatched area is trimmed off`
            : '';
        hint.classList.toggle('d-none', !round);
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
            // Emphasis only when the printer would honour it. Rendering it
            // here regardless is what made this preview a promise the label
            // did not keep: the browser showed bold, the print showed
            // asterisks. See src/utils/text_markup.py.
            const markup = typeof textMarkupEnabled === 'function' && textMarkupEnabled();
            let formattedText = escapeHtml(text).replace(/\n/g, '<br>');
            if (markup) {
                formattedText = formattedText
                    .replace(/\*\*\*(?=\S)(.+?)(?<=\S)\*\*\*/g, '<strong><em>$1</em></strong>')
                    .replace(/\*\*(?=\S)(.+?)(?<=\S)\*\*/g, '<strong>$1</strong>')
                    .replace(/(?<!\*)\*(?=\S)([^*]+?)(?<=\S)\*(?!\*)/g, '<em>$1</em>');
            }
            
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
            // Same rule as the text preview above: emphasis only where the
            // printer honours it, and the text escaped either way.
            const markup = typeof textMarkupEnabled === 'function' && textMarkupEnabled();
            let formattedText = escapeHtml(textContent).replace(/\n/g, '<br>');
            if (markup) {
                formattedText = formattedText
                    .replace(/\*\*\*(?=\S)(.+?)(?<=\S)\*\*\*/g, '<strong><em>$1</em></strong>')
                    .replace(/\*\*(?=\S)(.+?)(?<=\S)\*\*/g, '<strong>$1</strong>')
                    .replace(/(?<!\*)\*(?=\S)([^*]+?)(?<=\S)\*(?!\*)/g, '<em>$1</em>');
            }
            
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
 * Handle the Text + Image preview when an image file is selected. Reuses the
 * shared #preview-image element to show the chosen image (always in color,
 * since the text is rendered server-side). The placeholder/other previews are
 * hidden while it is shown.
 * @param {Event} event - Change event
 */
function handleTextImagePreview(event) {
    const previewImage = document.getElementById('preview-image');
    const previewPlaceholder = document.getElementById('preview-placeholder');

    if (!previewImage) return;

    if (event.target.files && event.target.files[0]) {
        const reader = new FileReader();

        reader.onload = function(e) {
            previewImage.dataset.originalSrc = e.target.result;
            previewImage.src = e.target.result;
            previewImage.style.filter = 'none';
            previewImage.classList.remove('d-none');

            if (previewPlaceholder) previewPlaceholder.classList.add('d-none');
            hideOtherPreviews('preview-image');
        };

        reader.readAsDataURL(event.target.files[0]);
    } else {
        previewImage.src = '';
        previewImage.dataset.originalSrc = '';
        previewImage.classList.add('d-none');

        if (areAllPreviewsEmpty() && previewPlaceholder) {
            previewPlaceholder.classList.remove('d-none');
        }
    }
}

/**
 * The 0-255 pixel cutoff the printer will actually use, derived from the 0-100
 * Threshold setting exactly the way the server does it.
 *
 * These are two different scales for the same idea and they have to agree.
 * `brother_ql.conversion.convert` maps the setting to a pixel value as
 * `(100 - threshold) * 255 / 100`, and `printer_service._to_print_appearance`
 * replicates that formula so the server-rendered preview matches the print.
 * This preview used a hard-coded 128 instead, i.e. a setting of 49.8 -- which
 * meant the Threshold field moved the print and the dial next to it, and left
 * the picture in the browser exactly where it was. A control that does nothing
 * visible is worse than no control.
 *
 * @returns {number} The cutoff in 0-255. Pixels at or above it stay white.
 */
function printThresholdCutoff() {
    const field = document.getElementById('threshold');
    // Same fallback as the server (`settings.get("threshold", 70)`), for the
    // case where this runs before the settings have loaded.
    let threshold = field ? parseFloat(field.value) : NaN;
    if (!isFinite(threshold)) threshold = 70;
    return (100 - threshold) * 255 / 100;
}

/**
 * Convert one pixel to the grayscale value the server would see.
 *
 * Pillow's `convert("L")` -- the first step of the server's black/white
 * rendering -- uses the ITU-R 601-2 luma coefficients, not the plain average of
 * the three channels this preview used to take. On a red logo the two disagree
 * by a wide margin (average 85, luma 76 for pure red), which is enough to land
 * on opposite sides of the cutoff and show a shape the printer will not print.
 *
 * @param {number} r
 * @param {number} g
 * @param {number} b
 * @returns {number} Luminance in 0-255.
 */
function toGrayLevel(r, g, b) {
    return (r * 299 + g * 587 + b * 114) / 1000;
}

/**
 * Re-run the current Image Mode over the loaded preview image.
 *
 * Called when a setting that feeds the conversion changes (Threshold), so the
 * black/white preview answers the dial immediately instead of waiting for the
 * next file selection.
 */
function refreshImagePreviewMode() {
    const previewImage = document.getElementById('preview-image');
    const imageMode = document.getElementById('image-mode');
    if (!previewImage || !imageMode) return;
    if (!previewImage.dataset.originalSrc) return;
    applyImageMode(previewImage, imageMode.value);
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
            // Hard threshold, on the printer's own terms: grayscale by luma,
            // then the cutoff the Threshold setting works out to. "At or above
            // stays white" matches the server's `p >= cutoff` comparison, so a
            // pixel sitting exactly on the boundary falls the same way in both.
            const cutoff = printThresholdCutoff();
            for (let i = 0; i < data.length; i += 4) {
                const gray = toGrayLevel(data[i], data[i + 1], data[i + 2]);
                const val = gray >= cutoff ? 255 : 0;
                data[i] = data[i + 1] = data[i + 2] = val;
            }
        } else if (mode === 'bw-dither') {
            // Floyd-Steinberg dithering.
            //
            // Dithering deliberately ignores the Threshold setting, because the
            // server does: with `dither` on it calls Pillow's `convert("1")`,
            // whose error diffusion is fixed at the 128 midpoint and takes no
            // threshold argument. Error diffusion is what decides how dark the
            // result is here, so a second dial on top of it would only make the
            // preview disagree with the print again -- in the other direction.
            const width = canvas.width;
            const height = canvas.height;

            // Convert to grayscale first, with the same luma weights as above.
            for (let i = 0; i < data.length; i += 4) {
                const gray = toGrayLevel(data[i], data[i + 1], data[i + 2]);
                data[i] = data[i + 1] = data[i + 2] = gray;
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
