import base64
import hashlib
import json
import secrets
import time
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from .db import add, auth_sessions, oauth_states, uid

ROLES = {'Release.Reader', 'Release.Operator', 'Release.Admin'}


class Identity:
    def __init__(self, settings, db):
        self.s, self.db = settings, db
        self.authority = f'https://login.microsoftonline.com/{settings.oidc_tenant}'
        self.issuer = self.authority + '/v2.0'
        self.jwks = jwt.PyJWKClient(self.authority + '/discovery/v2.0/keys', timeout=10)

    def verify(self, token, audience=None):
        try:
            key = self.jwks.get_signing_key_from_jwt(token)
            return jwt.decode(token, key.key, algorithms=['RS256'], audience=audience or self.s.oidc_audience,
                              issuer=self.issuer, options={'require': ['exp', 'iat', 'sub', 'iss', 'aud']})
        except (jwt.PyJWTError, ValueError) as exc:
            raise HTTPException(401, 'Invalid or expired company sign-in.') from exc

    def principal(self, request: Request):
        if self.s.demo:
            if self.s.api_key and not secrets.compare_digest(request.headers.get('X-API-Key', ''), self.s.api_key):
                raise HTTPException(401, 'Supply X-API-Key for this local demo.')
            return 'local-demo', ROLES
        authorization = request.headers.get('Authorization', '')
        if authorization.startswith('Bearer '):
            if not self.s.oidc_api_audience:
                raise HTTPException(401, 'Direct bearer API clients are not enabled.')
            claims = self.verify(authorization[7:], audience=self.s.oidc_api_audience)
            if 'Release.Access' not in claims.get('scp', '').split() and claims.get('idtyp') != 'app':
                raise HTTPException(403, 'A delegated API scope or application access token is required.')
            return claims.get('oid', claims['sub']), set(claims.get('roles', [])) & ROLES
        token = request.cookies.get('release_session', '')
        session = self.db.one(select(auth_sessions).where(
            auth_sessions.c.token_hash == hashlib.sha256(token.encode()).hexdigest(),
            auth_sessions.c.expires_at > int(time.time()))) if token else None
        if not session:
            raise HTTPException(401, 'Sign in with your company account.')
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            origin = request.headers.get('Origin')
            if origin != self.s.public_url:
                raise HTTPException(403, 'Request origin does not match the application URL.')
        return session['principal'], set(json.loads(session['roles']))

    def login(self):
        if self.s.demo:
            return RedirectResponse('/')
        state, nonce, verifier = uid(), uid(), secrets.token_urlsafe(48)
        with self.db.connect() as conn:
            add(conn, oauth_states, id=state, nonce=nonce, verifier=verifier,
                expires_at=int(time.time()) + 600, used=0)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        query = urlencode({'client_id': self.s.oidc_audience, 'response_type': 'code',
                           'redirect_uri': self.s.public_url + '/auth/callback', 'response_mode': 'query',
                           'scope': 'openid profile email', 'state': state, 'nonce': nonce,
                           'code_challenge': challenge, 'code_challenge_method': 'S256'})
        response = RedirectResponse(self.authority + '/oauth2/v2.0/authorize?' + query)
        response.set_cookie('release_oauth_state', state, secure=True, httponly=True, samesite='lax', max_age=600)
        return response

    def callback(self, request, code, state):
        if not secrets.compare_digest(request.cookies.get('release_oauth_state', ''), state):
            raise HTTPException(400, 'Sign-in state mismatch.')
        item = self.db.get(oauth_states, state)
        if not item or item['expires_at'] < time.time():
            raise HTTPException(400, 'Sign-in request expired.')
        with self.db.connect() as conn:
            claimed = conn.execute(oauth_states.update().where(oauth_states.c.id == state, oauth_states.c.used == 0)
                                   .values(used=1)).rowcount
        if claimed != 1:
            raise HTTPException(400, 'Sign-in request already used.')
        with httpx.Client(timeout=20) as client:
            response = client.post(self.authority + '/oauth2/v2.0/token', data={
                'client_id': self.s.oidc_audience, 'client_secret': self.s.oidc_secret,
                'grant_type': 'authorization_code', 'code': code, 'code_verifier': item['verifier'],
                'redirect_uri': self.s.public_url + '/auth/callback'})
        if response.status_code != 200:
            raise HTTPException(401, 'Company sign-in exchange failed.')
        claims = self.verify(response.json()['id_token'])
        if not secrets.compare_digest(claims.get('nonce', ''), item['nonce']):
            raise HTTPException(401, 'Sign-in nonce mismatch.')
        roles = set(claims.get('roles', [])) & ROLES
        if not roles:
            raise HTTPException(403, 'Ask the application owner to assign a Release role in Entra.')
        token = secrets.token_urlsafe(48)
        with self.db.connect() as conn:
            add(conn, auth_sessions, token_hash=hashlib.sha256(token.encode()).hexdigest(),
                principal=claims.get('oid', claims['sub']), roles=json.dumps(sorted(roles)),
                expires_at=min(int(claims['exp']), int(time.time()) + 28800))
        result = RedirectResponse('/', status_code=303)
        result.delete_cookie('release_oauth_state')
        result.set_cookie('release_session', token, httponly=True, secure=True, samesite='lax',
                          max_age=min(int(claims['exp']) - int(time.time()), 28800))
        return result
