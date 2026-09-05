from flask import Flask, request, jsonify, session, send_from_directory
from flask_cors import CORS
from functools import wraps
import os
import uuid
import re
from datetime import datetime, timedelta
import secrets
import string
import traceback
import json
import logging

# Firebase imports
import firebase_admin
from firebase_admin import credentials, firestore, storage

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='.', static_url_path='')
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
app.config['SESSION_COOKIE_SAMESITE'] = 'None'
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024  # 20MB

# CORS configuration
CORS(app, 
     origins=['http://localhost:3000', 'http://localhost:5500', 'http://127.0.0.1:5500', 
              'http://localhost:5000', 'http://127.0.0.1:5000', 
              'https://your-app.onrender.com', '*'],
     supports_credentials=True, 
     allow_headers=['Content-Type', 'Authorization', 'Accept'],
     methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'])

# ==================== Firebase Initialization ====================

def init_firebase():
    """Initialize Firebase with credentials from environment or file"""
    try:
        # Try environment variable first (for Render.com)
        firebase_config = os.environ.get('FIREBASE_CONFIG')
        if firebase_config:
            logger.info("Loading Firebase config from environment variable")
            cred_dict = json.loads(firebase_config)
            cred = credentials.Certificate(cred_dict)
        else:
            # Try local file (for development)
            cred_path = os.environ.get('FIREBASE_CREDENTIALS_PATH', 'firebase-credentials.json')
            if os.path.exists(cred_path):
                logger.info(f"Loading Firebase config from {cred_path}")
                cred = credentials.Certificate(cred_path)
            else:
                logger.error("Firebase credentials not found")
                return None, None, None

        # Initialize Firebase
        firebase_admin.initialize_app(cred, {
            'storageBucket': os.environ.get('FIREBASE_STORAGE_BUCKET', 'your-project.firebasestorage.app')
        })
        
        db = firestore.client()
        bucket = storage.bucket()
        logger.info("✅ Firebase initialized successfully")
        return db, bucket, True
        
    except Exception as e:
        logger.error(f"❌ Firebase initialization failed: {e}")
        return None, None, False

# Initialize Firebase
db, bucket, firebase_initialized = init_firebase()

# Default user for demo mode
DEFAULT_USER_EMAIL = os.environ.get('DEFAULT_USER_EMAIL', 'demo@printsafe.com')

# ===================== Firebase Models =====================

def ensure_user_exists(email):
    """Ensure a user exists in Firestore"""
    if not db:
        return {'id': 'demo_user', 'name': 'Demo User', 'email': email}
    
    try:
        users_ref = db.collection('users')
        query = users_ref.where('email', '==', email.lower()).limit(1)
        docs = query.stream()
        
        for doc in docs:
            return {'id': doc.id, **doc.to_dict()}
        
        # Create user if doesn't exist
        user_data = {
            'name': 'Demo User',
            'email': email.lower(),
            'created_at': firestore.SERVER_TIMESTAMP,
            'is_demo': True
        }
        doc_ref = users_ref.document()
        doc_ref.set(user_data)
        logger.info(f"Created demo user: {email}")
        return {'id': doc_ref.id, **user_data}
        
    except Exception as e:
        logger.error(f"Error ensuring user: {e}")
        return {'id': 'demo_user', 'name': 'Demo User', 'email': email}

def get_documents(owner_email):
    """Get all documents for a user from Firestore"""
    if not db:
        return []
    
    try:
        docs_ref = db.collection('documents')
        query = docs_ref.where('owner_email', '==', owner_email.lower())
        docs = query.stream()
        return [{'id': doc.id, **doc.to_dict()} for doc in docs]
    except Exception as e:
        logger.error(f"Error getting documents: {e}")
        return []

def get_document_by_id(doc_id, owner_email):
    """Get a specific document by ID from Firestore"""
    if not db:
        return None
    
    try:
        doc_ref = db.collection('documents').document(doc_id)
        doc = doc_ref.get()
        if doc.exists:
            data = doc.to_dict()
            if data.get('owner_email') == owner_email.lower():
                return {'id': doc.id, **data}
        return None
    except Exception as e:
        logger.error(f"Error getting document: {e}")
        return None

def create_document(owner_email, name, size, file_data):
    """Create a new document with file stored in Firebase Storage"""
    if not db or not bucket:
        logger.warning("Firebase not initialized, using mock storage")
        doc_id = str(uuid.uuid4())
        return {
            'id': doc_id,
            'name': name,
            'size': size,
            'owner_email': owner_email.lower(),
            'upload_date': datetime.now().isoformat(),
            'file_url': f'mock://{doc_id}/{name}',
            'file_path': None
        }
    
    try:
        doc_id = str(uuid.uuid4())
        timestamp = datetime.now().isoformat()
        
        # Upload file to Firebase Storage
        file_path = f"documents/{owner_email}/{doc_id}/{name}"
        blob = bucket.blob(file_path)
        blob.upload_from_string(file_data, content_type='application/pdf')
        
        # Make file publicly accessible
        blob.make_public()
        file_url = blob.public_url
        
        doc_data = {
            'name': name,
            'size': size,
            'owner_email': owner_email.lower(),
            'upload_date': timestamp,
            'file_url': file_url,
            'file_path': file_path,
            'pages': None
        }
        
        doc_ref = db.collection('documents').document(doc_id)
        doc_ref.set(doc_data)
        
        logger.info(f"Document created: {doc_id} - {name}")
        return {'id': doc_id, **doc_data}
        
    except Exception as e:
        logger.error(f"Error creating document: {e}")
        return None

def delete_document(doc_id, owner_email):
    """Delete a document and its file from Firebase"""
    if not db:
        return False
    
    try:
        doc = get_document_by_id(doc_id, owner_email)
        if not doc:
            return False
        
        # Delete from Storage
        if bucket and doc.get('file_path'):
            try:
                blob = bucket.blob(doc['file_path'])
                blob.delete()
                logger.info(f"Deleted file: {doc['file_path']}")
            except Exception as e:
                logger.warning(f"Storage delete error: {e}")
        
        # Delete from Firestore
        doc_ref = db.collection('documents').document(doc_id)
        doc_ref.delete()
        logger.info(f"Document deleted: {doc_id}")
        return True
        
    except Exception as e:
        logger.error(f"Error deleting document: {e}")
        return False

def get_links(owner_email):
    """Get all links for a user from Firestore"""
    if not db:
        return []
    
    try:
        links_ref = db.collection('print_links')
        query = links_ref.where('owner_email', '==', owner_email.lower())
        docs = query.stream()
        return [{'id': doc.id, **doc.to_dict()} for doc in docs]
    except Exception as e:
        logger.error(f"Error getting links: {e}")
        return []

def get_link_by_token(token):
    """Get a link by token from Firestore"""
    if not db:
        return None
    
    try:
        links_ref = db.collection('print_links')
        query = links_ref.where('token', '==', token).limit(1)
        docs = query.stream()
        for doc in docs:
            return {'id': doc.id, **doc.to_dict()}
        return None
    except Exception as e:
        logger.error(f"Error getting link by token: {e}")
        return None

def get_links_for_document(doc_id, owner_email):
    """Get all links for a specific document from Firestore"""
    if not db:
        return []
    
    try:
        links_ref = db.collection('print_links')
        query = links_ref.where('doc_id', '==', doc_id).where('owner_email', '==', owner_email.lower())
        docs = query.stream()
        return [{'id': doc.id, **doc.to_dict()} for doc in docs]
    except Exception as e:
        logger.error(f"Error getting links for document: {e}")
        return []

def create_print_link(doc_id, doc_name, owner_email, minutes):
    """Create a new print link in Firestore"""
    try:
        token = generate_token()
        code = generate_code()
        now = datetime.now()
        expires_at = now + timedelta(minutes=minutes)
        
        link_data = {
            'doc_id': doc_id,
            'doc_name': doc_name,
            'owner_email': owner_email.lower(),
            'token': token,
            'code': code,
            'created_at': now.isoformat(),
            'expires_at': expires_at.isoformat(),
            'revoked': False
        }
        
        if db:
            doc_ref = db.collection('print_links').document()
            doc_ref.set(link_data)
            link_data['id'] = doc_ref.id
        else:
            link_data['id'] = f"link_{int(now.timestamp())}_{secrets.token_hex(4)}"
        
        logger.info(f"Print link created: {token}")
        return link_data
        
    except Exception as e:
        logger.error(f"Error creating print link: {e}")
        return None

def revoke_link(link_id, owner_email):
    """Revoke a print link in Firestore"""
    if not db:
        return True
    
    try:
        doc_ref = db.collection('print_links').document(link_id)
        doc = doc_ref.get()
        if doc.exists:
            data = doc.to_dict()
            if data.get('owner_email') == owner_email.lower():
                doc_ref.update({'revoked': True})
                logger.info(f"Link revoked: {link_id}")
                return True
        return False
    except Exception as e:
        logger.error(f"Error revoking link: {e}")
        return False

def record_print_event(link_id):
    """Record that a print link was used"""
    if not db:
        return {'success': True}
    
    try:
        event_data = {
            'link_id': link_id,
            'timestamp': datetime.now().isoformat()
        }
        doc_ref = db.collection('print_events').document()
        doc_ref.set(event_data)
        return {'id': doc_ref.id, **event_data}
    except Exception as e:
        logger.error(f"Error recording print event: {e}")
        return None

def get_document_file(doc_id, owner_email):
    """Get the actual file data from Firebase Storage"""
    if not db or not bucket:
        return None
    
    try:
        doc = get_document_by_id(doc_id, owner_email)
        if not doc:
            return None
        
        file_path = doc.get('file_path')
        if not file_path:
            return None
        
        blob = bucket.blob(file_path)
        if blob.exists():
            return blob.download_as_bytes()
        return None
    except Exception as e:
        logger.error(f"Error getting document file: {e}")
        return None

# ===================== Utilities =====================

def generate_token():
    chars = string.ascii_lowercase + string.digits
    return ''.join(secrets.choice(chars) for _ in range(24))

def generate_code():
    return ''.join(secrets.choice(string.digits) for _ in range(6))

def validate_email(email):
    if not email:
        return False
    return re.match(r'^[^\s@]+@[^\s@]+\.[^\s@]+$', email) is not None

# ===================== Routes =====================

@app.route('/', methods=['GET'])
def index():
    try:
        return send_from_directory('.', 'dashboard.html')
    except:
        return jsonify({
            'name': 'PrintSafe API',
            'version': '2.0.0',
            'status': 'running',
            'firebase': 'connected' if firebase_initialized else 'mock'
        }), 200

@app.route('/<path:filename>', methods=['GET'])
def serve_static(filename):
    if filename.endswith('.html') or filename.endswith('.css') or filename.endswith('.js'):
        try:
            return send_from_directory('.', filename)
        except:
            pass
    if filename.startswith('api/'):
        return jsonify({'error': 'API endpoint not found'}), 404
    return jsonify({'error': 'File not found'}), 404

@app.route('/api/health', methods=['GET'])
def health_check():
    return jsonify({
        'status': 'ok',
        'timestamp': datetime.now().isoformat(),
        'server': 'Flask API',
        'version': '2.0.0',
        'firebase': 'connected' if firebase_initialized else 'mock',
        'mode': 'demo'
    })

# ===================== Document Routes =====================

@app.route('/api/documents', methods=['GET'])
def list_documents():
    try:
        ensure_user_exists(DEFAULT_USER_EMAIL)
        docs = get_documents(DEFAULT_USER_EMAIL)
        return jsonify(docs), 200
    except Exception as e:
        logger.error(f"List documents error: {e}")
        return jsonify({'error': 'Failed to fetch documents'}), 500

@app.route('/api/documents', methods=['POST'])
def upload_document():
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'No file provided'}), 400
        
        file = request.files['file']
        if file.filename == '':
            return jsonify({'error': 'No file selected'}), 400
        
        if not file.filename.lower().endswith('.pdf'):
            return jsonify({'error': 'Only PDF files are allowed'}), 400
        
        file.seek(0, 2)
        file_size = file.tell()
        file.seek(0)
        
        if file_size > 20 * 1024 * 1024:
            return jsonify({'error': 'File size exceeds 20MB limit'}), 400
        
        if file_size == 0:
            return jsonify({'error': 'File is empty'}), 400
        
        file_data = file.read()
        ensure_user_exists(DEFAULT_USER_EMAIL)
        
        doc = create_document(
            owner_email=DEFAULT_USER_EMAIL,
            name=file.filename,
            size=file_size,
            file_data=file_data
        )
        
        if doc:
            return jsonify(doc), 201
        
        return jsonify({'error': 'Upload failed'}), 500
        
    except Exception as e:
        logger.error(f"Upload error: {e}")
        logger.error(traceback.format_exc())
        return jsonify({'error': 'Server error'}), 500

@app.route('/api/documents/<doc_id>', methods=['DELETE'])
def delete_document_route(doc_id):
    try:
        ensure_user_exists(DEFAULT_USER_EMAIL)
        success = delete_document(doc_id, DEFAULT_USER_EMAIL)
        if success:
            return jsonify({'success': True, 'message': 'Document deleted successfully'}), 200
        return jsonify({'error': 'Document not found'}), 404
    except Exception as e:
        logger.error(f"Delete error: {e}")
        return jsonify({'error': 'Server error'}), 500

@app.route('/api/documents/<doc_id>/download', methods=['GET'])
def download_document(doc_id):
    try:
        file_data = get_document_file(doc_id, DEFAULT_USER_EMAIL)
        if not file_data:
            return jsonify({'error': 'Document not found'}), 404
        
        doc = get_document_by_id(doc_id, DEFAULT_USER_EMAIL)
        if not doc:
            return jsonify({'error': 'Document not found'}), 404
        
        return file_data, 200, {
            'Content-Type': 'application/pdf',
            'Content-Disposition': f'attachment; filename="{doc["name"]}"'
        }
    except Exception as e:
        logger.error(f"Download error: {e}")
        return jsonify({'error': 'Server error'}), 500

@app.route('/api/documents/<doc_id>', methods=['GET'])
def get_document(doc_id):
    try:
        doc = get_document_by_id(doc_id, DEFAULT_USER_EMAIL)
        if not doc:
            return jsonify({'error': 'Document not found'}), 404
        return jsonify(doc), 200
    except Exception as e:
        logger.error(f"Get document error: {e}")
        return jsonify({'error': 'Server error'}), 500

# ===================== Print Link Routes =====================

@app.route('/api/links', methods=['GET'])
def list_links():
    try:
        links = get_links(DEFAULT_USER_EMAIL)
        return jsonify(links), 200
    except Exception as e:
        logger.error(f"List links error: {e}")
        return jsonify({'error': 'Failed to fetch links'}), 500

@app.route('/api/links/<doc_id>', methods=['POST'])
def create_link(doc_id):
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'Invalid request data'}), 400
            
        minutes = data.get('minutes', 5)
        
        if minutes < 1 or minutes > 1440:
            return jsonify({'error': 'Minutes must be between 1 and 1440'}), 400
        
        doc = get_document_by_id(doc_id, DEFAULT_USER_EMAIL)
        if not doc:
            return jsonify({'error': 'Document not found'}), 404
        
        link = create_print_link(
            doc_id=doc_id,
            doc_name=doc['name'],
            owner_email=DEFAULT_USER_EMAIL,
            minutes=minutes
        )
        
        if link:
            return jsonify(link), 201
        
        return jsonify({'error': 'Failed to create link'}), 500
    except Exception as e:
        logger.error(f"Create link error: {e}")
        return jsonify({'error': 'Server error'}), 500

@app.route('/api/links/<link_id>/revoke', methods=['POST'])
def revoke_link_route(link_id):
    try:
        success = revoke_link(link_id, DEFAULT_USER_EMAIL)
        if success:
            return jsonify({'success': True, 'message': 'Link revoked successfully'}), 200
        return jsonify({'error': 'Link not found'}), 404
    except Exception as e:
        logger.error(f"Revoke link error: {e}")
        return jsonify({'error': 'Server error'}), 500

@app.route('/api/links/active/<doc_id>', methods=['GET'])
def get_active_link(doc_id):
    try:
        links = get_links_for_document(doc_id, DEFAULT_USER_EMAIL)
        active = [l for l in links if not l.get('revoked', False) and datetime.fromisoformat(l['expires_at']) > datetime.now()]
        if active:
            return jsonify(active[0]), 200
        return jsonify({'active': False}), 200
    except Exception as e:
        logger.error(f"Get active link error: {e}")
        return jsonify({'error': 'Server error'}), 500

# ===================== Public Print Routes =====================

@app.route('/api/print/<token>', methods=['GET'])
def get_print_link(token):
    try:
        if not token:
            return jsonify({'error': 'Token is required'}), 400
            
        link = get_link_by_token(token)
        if not link:
            return jsonify({'error': 'Link not found'}), 404
        
        if link.get('revoked', False):
            return jsonify({'error': 'Link has been revoked'}), 403
        
        expires_at = datetime.fromisoformat(link['expires_at'])
        if expires_at < datetime.now():
            return jsonify({'error': 'Link has expired'}), 410
        
        doc = get_document_by_id(link['doc_id'], link['owner_email'])
        if not doc:
            return jsonify({'error': 'Document not found'}), 404
        
        return jsonify({
            'link': link,
            'document': {
                'id': doc['id'],
                'name': doc['name'],
                'size': doc['size'],
                'file_url': doc['file_url']
            }
        }), 200
    except Exception as e:
        logger.error(f"Get print link error: {e}")
        return jsonify({'error': 'Server error'}), 500

@app.route('/api/print/<token>/event', methods=['POST'])
def print_event(token):
    try:
        if not token:
            return jsonify({'error': 'Token is required'}), 400
            
        link = get_link_by_token(token)
        if not link:
            return jsonify({'error': 'Link not found'}), 404
        
        if link.get('revoked', False):
            return jsonify({'error': 'Link revoked'}), 403
        
        expires_at = datetime.fromisoformat(link['expires_at'])
        if expires_at < datetime.now():
            return jsonify({'error': 'Link expired'}), 410
        
        record_print_event(link['id'])
        return jsonify({'success': True, 'message': 'Print event recorded'}), 200
    except Exception as e:
        logger.error(f"Print event error: {e}")
        return jsonify({'error': 'Server error'}), 500

@app.route('/api/print/<token>/file', methods=['GET'])
def get_print_file(token):
    try:
        if not token:
            return jsonify({'error': 'Token is required'}), 400
            
        link = get_link_by_token(token)
        if not link:
            return jsonify({'error': 'Link not found'}), 404
        
        if link.get('revoked', False):
            return jsonify({'error': 'Link has been revoked'}), 403
        
        expires_at = datetime.fromisoformat(link['expires_at'])
        if expires_at < datetime.now():
            return jsonify({'error': 'Link has expired'}), 410
        
        file_data = get_document_file(link['doc_id'], link['owner_email'])
        if not file_data:
            return jsonify({'error': 'Document not found'}), 404
        
        doc = get_document_by_id(link['doc_id'], link['owner_email'])
        if not doc:
            return jsonify({'error': 'Document not found'}), 404
        
        return file_data, 200, {
            'Content-Type': 'application/pdf',
            'Content-Disposition': f'inline; filename="{doc["name"]}"'
        }
    except Exception as e:
        logger.error(f"Get print file error: {e}")
        return jsonify({'error': 'Server error'}), 500

# ===================== Watcher Status =====================

@app.route('/api/watcher/status', methods=['GET'])
def watcher_status():
    watcher_running = os.environ.get('PDF_WATCHER_RUNNING', 'false').lower() == 'true'
    return jsonify({
        'running': watcher_running,
        'port': 8765,
        'delete_delay': 5
    }), 200

@app.route('/api/watcher/schedule', methods=['POST'])
def schedule_deletion():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'Invalid request data'}), 400
            
        filename = data.get('filename')
        if not filename:
            return jsonify({'error': 'Filename required'}), 400
        
        return jsonify({
            'status': 'scheduled',
            'filename': filename,
            'delete_delay': 5,
            'scheduled_at': datetime.now().isoformat()
        }), 200
    except Exception as e:
        logger.error(f"Schedule deletion error: {e}")
        return jsonify({'error': 'Server error'}), 500

# ===================== Error Handlers =====================

@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Endpoint not found'}), 404

@app.errorhandler(405)
def method_not_allowed(error):
    return jsonify({'error': 'Method not allowed'}), 405

@app.errorhandler(500)
def internal_error(error):
    return jsonify({'error': 'Internal server error'}), 500

# ===================== Main =====================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    
    print("\n" + "="*60)
    print("📄 PrintSafe API Server")
    print("="*60)
    print(f"🌐 Port: {port}")
    print(f"🐛 Debug: {debug}")
    print(f"🔥 Firebase: {'Connected' if firebase_initialized else 'Mock Mode'}")
    print(f"👤 Demo User: {DEFAULT_USER_EMAIL}")
    print("\n📡 Routes:")
    print("  GET  /")
    print("  GET  /api/health")
    print("  GET  /api/documents")
    print("  POST /api/documents")
    print("  GET  /api/documents/<id>")
    print("  DELETE /api/documents/<id>")
    print("  GET  /api/documents/<id>/download")
    print("  GET  /api/links")
    print("  POST /api/links/<doc_id>")
    print("  POST /api/links/<link_id>/revoke")
    print("  GET  /api/links/active/<doc_id>")
    print("  GET  /api/print/<token>")
    print("  POST /api/print/<token>/event")
    print("  GET  /api/print/<token>/file")
    print("="*60)
    print(f"\n🚀 Server running at: http://localhost:{port}")
    print(f"📄 Dashboard: http://localhost:{port}/dashboard.html")
    print("="*60)
    
    app.run(host='0.0.0.0', port=port, debug=debug)