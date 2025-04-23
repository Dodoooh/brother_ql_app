// Brother QL Printer App - Utility Functions

/**
 * Show a notification to the user
 * @param {string} message - The message to display
 * @param {string} type - The type of notification (success, error, info, warning)
 * @param {number} duration - How long to show the notification in milliseconds
 */
function showNotification(message, type = 'info', duration = 5000) {
    // Create notification container if it doesn't exist
    let notificationsContainer = document.querySelector('.notifications');
    if (!notificationsContainer) {
        notificationsContainer = document.createElement('div');
        notificationsContainer.className = 'notifications';
        document.body.appendChild(notificationsContainer);
    }
    
    // Create notification element
    const notification = document.createElement('div');
    notification.className = `notification ${type}`;
    
    // Create notification content
    const icon = getNotificationIcon(type);
    notification.innerHTML = `
        <div class="d-flex align-items-center">
            <i class="${icon} me-2"></i>
            <div>${message}</div>
            <button type="button" class="notification-close ms-3" aria-label="Close">
                <i class="bi bi-x"></i>
            </button>
        </div>
    `;
    
    // Add notification to container
    notificationsContainer.appendChild(notification);
    
    // Add event listener to close button
    const closeButton = notification.querySelector('.notification-close');
    if (closeButton) {
        closeButton.addEventListener('click', () => {
            removeNotification(notification);
        });
    }
    
    // Auto-remove notification after duration
    setTimeout(() => {
        removeNotification(notification);
    }, duration);
}

/**
 * Get the appropriate icon for a notification type
 * @param {string} type - The type of notification
 * @returns {string} - The icon class
 */
function getNotificationIcon(type) {
    switch (type) {
        case 'success':
            return 'bi bi-check-circle-fill';
        case 'error':
            return 'bi bi-exclamation-circle-fill';
        case 'warning':
            return 'bi bi-exclamation-triangle-fill';
        case 'info':
        default:
            return 'bi bi-info-circle-fill';
    }
}

/**
 * Remove a notification with animation
 * @param {HTMLElement} notification - The notification element to remove
 */
function removeNotification(notification) {
    // Add fade-out animation
    notification.style.opacity = '0';
    notification.style.transform = 'translateX(100%)';
    
    // Remove element after animation completes
    setTimeout(() => {
        if (notification.parentElement) {
            notification.parentElement.removeChild(notification);
        }
    }, 300);
}

/**
 * Format a date object to a readable string
 * @param {Date} date - The date to format
 * @returns {string} - The formatted date string
 */
function formatDate(date) {
    if (!date) return '';
    
    const options = {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit'
    };
    
    return date.toLocaleDateString(undefined, options);
}

/**
 * Debounce a function to prevent it from being called too frequently
 * @param {Function} func - The function to debounce
 * @param {number} wait - The debounce wait time in milliseconds
 * @returns {Function} - The debounced function
 */
function debounce(func, wait = 300) {
    let timeout;
    
    return function executedFunction(...args) {
        const later = () => {
            clearTimeout(timeout);
            func(...args);
        };
        
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
    };
}

/**
 * Validate a URL
 * @param {string} url - The URL to validate
 * @returns {boolean} - Whether the URL is valid
 */
function isValidUrl(url) {
    try {
        new URL(url);
        return true;
    } catch (error) {
        return false;
    }
}

/**
 * Validate printer settings
 * @param {Object} settings - The printer settings to validate
 * @returns {Object} - Validation result with isValid and message properties
 */
function validatePrinterSettings(settings) {
    if (!settings) {
        return { isValid: false, message: 'Settings are required' };
    }
    
    if (!settings.printer_uri) {
        return { isValid: false, message: 'Printer URI is required' };
    }
    
    if (!isValidUrl(settings.printer_uri)) {
        return { isValid: false, message: 'Printer URI is not a valid URL' };
    }
    
    if (!settings.printer_model) {
        return { isValid: false, message: 'Printer model is required' };
    }
    
    if (!settings.label_size) {
        return { isValid: false, message: 'Label size is required' };
    }
    
    return { isValid: true, message: 'Settings are valid' };
}

/**
 * Get common printer settings from form elements
 * @returns {Object} - The printer settings object
 */
function getPrinterSettings() {
    return {
        printer_uri: document.getElementById('printer-uri').value,
        printer_model: document.getElementById('printer-model').value,
        label_size: document.getElementById('label-size').value,
        rotate: parseInt(document.getElementById('rotate').value) || 0,
        threshold: parseFloat(document.getElementById('threshold').value) || 70,
        dither: document.getElementById('dither').value === 'true',
        red: document.getElementById('red').value === 'true'
    };
}
