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
    
    console.log('Brother QL Printer App initialized');
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
            
            // Update the appropriate preview based on the active tab
            if (targetId === '#text-panel') {
                updateTextPreview();
            } else if (targetId === '#image-panel') {
                // Image preview is handled by the file input change event
            } else if (targetId === '#qrcode-panel') {
                updateQRCodePreview();
            } else if (targetId === '#label-panel') {
                updateLabelPreview();
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
            });
        }
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
    }
    
    // Settings form
    const settingsForm = document.getElementById('settings-form');
    if (settingsForm) {
        settingsForm.addEventListener('submit', handleSaveSettings);
    }
}

/**
 * Initialize theme based on user preference
 */
function initTheme() {
    const savedTheme = localStorage.getItem('theme');
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    
    if (savedTheme === 'dark' || (!savedTheme && prefersDark)) {
        document.body.classList.add('dark-mode');
        updateThemeToggleIcon(true);
    } else {
        document.body.classList.remove('dark-mode');
        updateThemeToggleIcon(false);
    }
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
