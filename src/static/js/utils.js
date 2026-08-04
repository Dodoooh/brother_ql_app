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
    
    // Create notification content.
    //
    // The icon is the only part that changes colour with the type: the card
    // itself is identical for every severity, so a warning and a confirmation
    // are the same object saying different things rather than two designs.
    //
    // The message itself is set as *text*, never as markup. Almost every call
    // site here interpolates something the app did not write -- `error.message`
    // straight out of a server JSON body, the relay's warning sentence, a job
    // label -- and a printer name or a filename containing "<" is enough to
    // break the card even without anyone meaning harm. The scaffolding around
    // it is still built as markup because it is a constant, so the two are kept
    // apart: the frame is written once here, the message only ever fills a text
    // node. Any caller that wants emphasis has to gain a separate parameter for
    // it; none does today.
    const icon = getNotificationIcon(type);
    notification.innerHTML = `
        <div class="d-flex align-items-center">
            <i class="${icon} notification-icon me-2" aria-hidden="true"></i>
            <div class="notification-message"></div>
            <button type="button" class="notification-close ms-3" aria-label="Close">
                <i class="bi bi-x"></i>
            </button>
        </div>
    `;
    const messageEl = notification.querySelector('.notification-message');
    if (messageEl) messageEl.textContent = message == null ? '' : String(message);

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

