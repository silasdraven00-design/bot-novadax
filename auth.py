import jwt
import bcrypt
import json
import os
from datetime import datetime, timedelta
from functools import wraps
from flask import request, jsonify

# ─────────────────────────────────────────────
# Configurações
# ─────────────────────────────────────────────
JWT_SECRET = os.environ.get('JWT_SECRET', 'TROQUE_ESTA_CHAVE_SECRETA_123!')
JWT_EXPIRY  = 24  # horas

USERS_FILE = 'users.json'

# Usuários padrão — altere as senhas antes de subir!
DEFAULT_USERS = {
    'silas': {
        'password_hash': bcrypt.hashpw(b'senha123', bcrypt.gensalt()).decode(),
        'role': 'admin',
        'name': 'Silas'
    },
    'caique': {
        'password_hash': bcrypt.hashpw(b'senha456', bcrypt.gensalt()).decode(),
        'role': 'viewer',
        'name': 'Caíque'
    },
    'roque': {
        'password_hash': bcrypt.hashpw(b'senha789', bcrypt.gensalt()).decode(),
        'role': 'admin',
        'name': 'Roque'
    }
}

# ─────────────────────────────────────────────
# Gerenciamento de usuários
# ─────────────────────────────────────────────
def carregar_usuarios():
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, 'r') as f:
            return json.load(f)
    # Cria arquivo com usuários padrão
    salvar_usuarios(DEFAULT_USERS)
    return DEFAULT_USERS

def salvar_usuarios(users):
    with open(USERS_FILE, 'w') as f:
        json.dump(users, f, indent=2)

# ─────────────────────────────────────────────
# JWT
# ─────────────────────────────────────────────
def gerar_token(username, role):
    payload = {
        'username': username,
        'role':     role,
        'exp':      datetime.utcnow() + timedelta(hours=JWT_EXPIRY),
        'iat':      datetime.utcnow()
    }
    return jwt.encode(payload, JWT_SECRET, algorithm='HS256')

def verificar_token(token):
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None

# ─────────────────────────────────────────────
# Decorators
# ─────────────────────────────────────────────
def requer_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        auth_header = request.headers.get('Authorization')
        if auth_header and auth_header.startswith('Bearer '):
            token = auth_header.split(' ')[1]

        if not token:
            return jsonify({'error': 'Token necessário'}), 401

        payload = verificar_token(token)
        if not payload:
            return jsonify({'error': 'Token inválido ou expirado'}), 401

        request.user = payload
        return f(*args, **kwargs)
    return decorated

def requer_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        auth_header = request.headers.get('Authorization')
        if auth_header and auth_header.startswith('Bearer '):
            token = auth_header.split(' ')[1]

        if not token:
            return jsonify({'error': 'Token necessário'}), 401

        payload = verificar_token(token)
        if not payload:
            return jsonify({'error': 'Token inválido ou expirado'}), 401

        if payload.get('role') != 'admin':
            return jsonify({'error': 'Acesso restrito ao administrador'}), 403

        request.user = payload
        return f(*args, **kwargs)
    return decorated

# ─────────────────────────────────────────────
# Rotas de autenticação
# ─────────────────────────────────────────────
def registrar_rotas_auth(app):

    @app.route('/auth/login', methods=['POST'])
    def login():
        data = request.get_json()
        if not data:
            return jsonify({'error': 'Dados inválidos'}), 400

        username = data.get('username', '').strip().lower()
        password = data.get('password', '').encode()

        users = carregar_usuarios()
        user  = users.get(username)

        if not user:
            return jsonify({'error': 'Usuário ou senha incorretos'}), 401

        if not bcrypt.checkpw(password, user['password_hash'].encode()):
            return jsonify({'error': 'Usuário ou senha incorretos'}), 401

        token = gerar_token(username, user['role'])
        return jsonify({
            'token':    token,
            'username': username,
            'role':     user['role'],
            'name':     user['name'],
            'expires':  JWT_EXPIRY
        })

    @app.route('/auth/verificar', methods=['GET'])
    @requer_auth
    def verificar():
        return jsonify({
            'valid':    True,
            'username': request.user['username'],
            'role':     request.user['role']
        })

    @app.route('/auth/trocar-senha', methods=['POST'])
    @requer_auth
    def trocar_senha():
        data = request.get_json()
        senha_atual = data.get('senha_atual', '').encode()
        senha_nova  = data.get('senha_nova', '').encode()

        if len(senha_nova) < 6:
            return jsonify({'error': 'Senha nova precisa ter pelo menos 6 caracteres'}), 400

        users    = carregar_usuarios()
        username = request.user['username']
        user     = users[username]

        if not bcrypt.checkpw(senha_atual, user['password_hash'].encode()):
            return jsonify({'error': 'Senha atual incorreta'}), 401

        users[username]['password_hash'] = bcrypt.hashpw(senha_nova, bcrypt.gensalt()).decode()
        salvar_usuarios(users)
        return jsonify({'success': True, 'message': 'Senha alterada com sucesso'})
