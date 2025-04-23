"""
Patch for compatibility with newer versions of Pillow.
"""

import PIL.Image
import logging

logger = logging.getLogger(__name__)

def apply_pillow_patch():
    """
    Apply patch for compatibility with newer versions of Pillow.
    
    In newer versions of Pillow, Image.ANTIALIAS is deprecated and replaced with
    Image.Resampling.LANCZOS. This patch adds the ANTIALIAS attribute to the Image
    module if it doesn't exist.
    """
    if not hasattr(PIL.Image, 'ANTIALIAS'):
        logger.info("Applying Pillow patch: Adding ANTIALIAS attribute")
        # Use LANCZOS as a replacement for ANTIALIAS
        if hasattr(PIL.Image, 'Resampling') and hasattr(PIL.Image.Resampling, 'LANCZOS'):
            PIL.Image.ANTIALIAS = PIL.Image.Resampling.LANCZOS
        else:
            # Fallback for older versions that might have LANCZOS directly
            PIL.Image.ANTIALIAS = PIL.Image.LANCZOS if hasattr(PIL.Image, 'LANCZOS') else PIL.Image.BICUBIC
