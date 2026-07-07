import os
import json
import time
import hashlib
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, Response
import requests
import redis
from functools import wraps

app = Flask(__name__)

# Configuration
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379')
ADMIN_KEY = os.environ.get('ADMIN_KEY', 'paid')  # Default admin key
API_DEVELOPER = "@VerifiedNur"
ORIGINAL_API_URL = "https://paid.originalapis.workers.dev/tg"
ORIGINAL_API_KEY = "primexnur"  # The key for the original API

# Initialize Redis connection
try:
    redis_client = redis.from_url(REDIS_URL)
    redis_client.ping()
    print("Redis connected successfully!")
except Exception as e:
    print(f"Redis connection error: {e}")
    # Fallback to in-memory storage if Redis is not available
    redis_client = None

# In-memory storage as fallback
in_memory_storage = {
    'keys': {},  # {key: expiry_timestamp}
    'usage': {}  # {key: usage_count}
}

# Constants
DEFAULT_KEY_DURATION = 30  # days
MAX_USAGE_PER_DAY = 100  # Maximum usage per key per day

class KeyManager:
    @staticmethod
    def set_key(key, duration_days):
        """Add or update a key with expiration"""
        expiry = int((datetime.now() + timedelta(days=duration_days)).timestamp())
        
        if redis_client:
            redis_client.hset('telegram_keys', key, expiry)
            redis_client.hset('telegram_usage', key, 0)
        else:
            in_memory_storage['keys'][key] = expiry
            in_memory_storage['usage'][key] = 0
        
        return True
    
    @staticmethod
    def remove_key(key):
        """Remove a key"""
        if redis_client:
            redis_client.hdel('telegram_keys', key)
            redis_client.hdel('telegram_usage', key)
        else:
            in_memory_storage['keys'].pop(key, None)
            in_memory_storage['usage'].pop(key, None)
        
        return True
    
    @staticmethod
    def get_key_expiry(key):
        """Get key expiry timestamp"""
        if redis_client:
            expiry = redis_client.hget('telegram_keys', key)
            return int(expiry) if expiry else None
        else:
            return in_memory_storage['keys'].get(key)
    
    @staticmethod
    def increment_usage(key):
        """Increment usage count for a key"""
        if redis_client:
            usage = redis_client.hincrby('telegram_usage', key, 1)
            return int(usage)
        else:
            if key not in in_memory_storage['usage']:
                in_memory_storage['usage'][key] = 0
            in_memory_storage['usage'][key] += 1
            return in_memory_storage['usage'][key]
    
    @staticmethod
    def get_usage(key):
        """Get usage count for a key"""
        if redis_client:
            usage = redis_client.hget('telegram_usage', key)
            return int(usage) if usage else 0
        else:
            return in_memory_storage['usage'].get(key, 0)
    
    @staticmethod
    def reset_daily_usage():
        """Reset daily usage (to be called daily)"""
        if redis_client:
            # We'll just reset all keys
            all_keys = redis_client.hkeys('telegram_usage')
            for key in all_keys:
                redis_client.hset('telegram_usage', key, 0)
        else:
            for key in in_memory_storage['usage']:
                in_memory_storage['usage'][key] = 0

def validate_key(key):
    """Validate if a key exists and is not expired"""
    if not key:
        return False
    
    expiry = KeyManager.get_key_expiry(key)
    if expiry is None:
        return False
    
    current_time = int(time.time())
    if current_time > expiry:
        # Key expired, remove it
        KeyManager.remove_key(key)
        return False
    
    return True

def check_usage_limit(key):
    """Check if usage limit is exceeded"""
    usage = KeyManager.get_usage(key)
    if usage >= MAX_USAGE_PER_DAY:
        return False
    return True

def call_original_api(user_id):
    """Call the original API with the user ID"""
    try:
        url = f"{ORIGINAL_API_URL}?key={ORIGINAL_API_KEY}&id={user_id}"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        app.logger.error(f"Error calling original API: {e}")
        return None

def require_valid_key(f):
    """Decorator to validate API key"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        api_key = request.args.get('key')
        if not api_key:
            return jsonify({
                "API_Developer": API_DEVELOPER,
                "error": "Missing API key"
            }), 400
        
        if not validate_key(api_key):
            return jsonify({
                "API_Developer": API_DEVELOPER,
                "error": "Invalid or expired API key"
            }), 401
        
        if not check_usage_limit(api_key):
            return jsonify({
                "API_Developer": API_DEVELOPER,
                "error": "Daily usage limit exceeded"
            }), 429
        
        return f(*args, **kwargs, api_key=api_key)
    return decorated_function

@app.route('/tg', methods=['GET'])
@require_valid_key
def get_telegram_info(api_key=None):
    """Main endpoint to get Telegram info"""
    user_id = request.args.get('id')
    if not user_id:
        return jsonify({
            "API_Developer": API_DEVELOPER,
            "error": "Missing user ID"
        }), 400
    
    # Increment usage
    current_usage = KeyManager.increment_usage(api_key)
    
    # Call original API
    original_response = call_original_api(user_id)
    
    if not original_response:
        return jsonify({
            "API_Developer": API_DEVELOPER,
            "Today_Used": current_usage,
            "error": "Failed to fetch data from original API"
        }), 503
    
    # Check if the original API returned an error
    if "result" in original_response and "results" in original_response["result"]:
        results = original_response["result"]["results"]
        
        # Check if the message indicates no data
        if "message" in results and "No data found" in results["message"]:
            return jsonify({
                "API_Developer": API_DEVELOPER,
                "Today_Used": current_usage,
                "result": {
                    "results": {
                        "message": "No data found for this Telegram ID"
                    }
                }
            })
        
        # Transform the response to our format
        transformed_response = {
            "API_Developer": API_DEVELOPER,
            "Today_Used": current_usage,
            "result": {
                "results": {
                    "tgid": results.get("tgid", user_id),
                    "number": results.get("number", ""),
                    "country": results.get("country", ""),
                    "country_code": results.get("country_code", "")
                }
            }
        }
        
        return jsonify(transformed_response)
    else:
        # Handle unexpected response format
        return jsonify({
            "API_Developer": API_DEVELOPER,
            "Today_Used": current_usage,
            "error": "Unexpected response from original API"
        }), 500

@app.route('/admin', methods=['GET'])
def admin_endpoint():
    """Admin endpoint for key management"""
    adminkey = request.args.get('adminkey')
    
    # Check if admin key is valid
    if adminkey != ADMIN_KEY:
        return jsonify({
            "API_Developer": API_DEVELOPER,
            "error": "Invalid admin key"
        }), 401
    
    key = request.args.get('key')
    value = request.args.get('value')  # Days to keep the key alive
    
    if not key:
        return jsonify({
            "API_Developer": API_DEVELOPER,
            "error": "Missing key parameter"
        }), 400
    
    # If value is provided, add/update key
    if value is not None:
        try:
            days = int(value)
            if days <= 0:
                return jsonify({
                    "API_Developer": API_DEVELOPER,
                    "error": "Value must be a positive integer"
                }), 400
            
            KeyManager.set_key(key, days)
            return jsonify({
                "API_Developer": API_DEVELOPER,
                "message": f"Key {key} added successfully for {days} days",
                "expiry_date": (datetime.now() + timedelta(days=days)).isoformat()
            })
        except ValueError:
            return jsonify({
                "API_Developer": API_DEVELOPER,
                "error": "Value must be a valid integer"
            }), 400
    else:
        # Remove key
        KeyManager.remove_key(key)
        return jsonify({
            "API_Developer": API_DEVELOPER,
            "message": f"Key {key} removed successfully"
        })

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat()
    })

@app.errorhandler(404)
def not_found(error):
    return jsonify({
        "API_Developer": API_DEVELOPER,
        "error": "Endpoint not found"
    }), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({
        "API_Developer": API_DEVELOPER,
        "error": "Internal server error"
    }), 500

# For Vercel serverless deployment
def handler(request):
    """Vercel serverless function handler"""
    return app(request)

# For local development
if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))