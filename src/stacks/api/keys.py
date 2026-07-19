import logging

from flask import (
    current_app,
    jsonify,
    request,
)

from . import api_bp
from stacks.security.auth import (
    require_session_only,
    generate_secret_key,
    validate_api_key,
)

logger = logging.getLogger("api")

@api_bp.route('/api/key/regenerate', methods=['POST'])
@require_session_only
def api_key_regenerate():
    """Regenerate admin API key"""
    try:
        new_key = generate_secret_key()
        config = current_app.stacks_config
        config.set('api', 'key', value=new_key)
        config.save()

        logger.info("Admin API key regenerated")

        return jsonify({
            'success': True,
            'message': 'New API key generated',
            'api_key': new_key
        })
    except Exception as e:
        logger.error(f"Failed to regenerate API key: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 400

@api_bp.route('/api/key/disable', methods=['POST'])
@require_session_only
def api_key_disable():
    """Disable admin API key (set to null)"""
    try:
        config = current_app.stacks_config
        config.set('api', 'key', value=None)
        config.save()

        logger.info("Admin API key disabled")

        return jsonify({
            'success': True,
            'message': 'Admin API key disabled'
        })
    except Exception as e:
        logger.error(f"Failed to disable admin API key: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 400

@api_bp.route('/api/key/downloader/regenerate', methods=['POST'])
@require_session_only
def api_key_downloader_regenerate():
    """Regenerate downloader API key"""
    try:
        new_key = generate_secret_key()
        config = current_app.stacks_config
        config.set('api', 'downloader_key', value=new_key)
        config.save()

        logger.info("Downloader API key regenerated")

        return jsonify({
            'success': True,
            'message': 'New downloader API key generated',
            'downloader_key': new_key
        })
    except Exception as e:
        logger.error(f"Failed to regenerate downloader API key: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 400

@api_bp.route('/api/key/downloader/disable', methods=['POST'])
@require_session_only
def api_key_downloader_disable():
    """Disable downloader API key (set to null)"""
    try:
        config = current_app.stacks_config
        config.set('api', 'downloader_key', value=None)
        config.save()

        logger.info("Downloader API key disabled")

        return jsonify({
            'success': True,
            'message': 'Downloader API key disabled'
        })
    except Exception as e:
        logger.error(f"Failed to disable downloader API key: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 400

@api_bp.route('/api/key')
@require_session_only
def api_key_info():
    """Get API keys (session auth only - for web UI)"""
    config = current_app.stacks_config
    return jsonify({
        'api_key': config.get('api', 'key'),
        'downloader_key': config.get('api', 'downloader_key', default=None)
    })

@api_bp.route('/api/key/test', methods=['POST'])
@require_session_only
def api_key_test():
    """
    Test an API key and return its type.
    No authentication required - this is for validating keys.

    Request body:
        {"key": "api_key_to_test"}

    Response:
        {"valid": true/false, "type": "admin"/"downloader"/null}
    """
    data = request.json or {}
    test_key = data.get('key')

    if not test_key:
        return jsonify({'valid': False, 'type': None})

    is_valid, key_type = validate_api_key(test_key)

    return jsonify({
        'valid': is_valid,
        'type': key_type
    })




