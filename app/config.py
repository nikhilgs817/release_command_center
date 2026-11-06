import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / '.env')


@dataclass
class Settings:
    db_path: str = os.getenv('DB_PATH', str(ROOT / 'data' / 'command-center-v1.sqlite3'))
    database_url: str = os.getenv('DATABASE_URL', '')
    demo: bool = os.getenv('DEMO_MODE', 'true').lower() == 'true'
    api_key: str = os.getenv('API_KEY', '')
    webhook_token: str = os.getenv('WEBHOOK_TOKEN', '')
    oidc_tenant: str = os.getenv('ENTRA_TENANT_ID', '')
    oidc_audience: str = os.getenv('ENTRA_AUDIENCE', '')
    oidc_api_audience: str = os.getenv('ENTRA_API_AUDIENCE', '')
    oidc_secret: str = os.getenv('ENTRA_CLIENT_SECRET', '')
    public_url: str = os.getenv('PUBLIC_URL', 'http://127.0.0.1:8000').rstrip('/')
    external_writes: bool = os.getenv('ALLOW_EXTERNAL_WRITES', 'false').lower() == 'true'
    sn_url: str = os.getenv('SERVICENOW_URL', '').rstrip('/')
    sn_user: str = os.getenv('SERVICENOW_USER', '')
    sn_token: str = os.getenv('SERVICENOW_TOKEN', '')
    sn_env_field: str = os.getenv('SERVICENOW_ENV_FIELD', '')
    jira_url: str = os.getenv('JIRA_URL', '').rstrip('/')
    jira_user: str = os.getenv('JIRA_USER', '')
    jira_token: str = os.getenv('JIRA_TOKEN', '')
    github_url: str = os.getenv('GITHUB_API_URL', 'https://api.github.com').rstrip('/')
    github_token: str = os.getenv('GITHUB_TOKEN', '')
    jenkins_url: str = os.getenv('JENKINS_URL', '').rstrip('/')
    jenkins_user: str = os.getenv('JENKINS_USER', '')
    jenkins_token: str = os.getenv('JENKINS_TOKEN', '')
    smtp_host: str = os.getenv('SMTP_HOST', '')
    smtp_port: int = int(os.getenv('SMTP_PORT', '587'))
    mail_user: str = os.getenv('MAIL_USER', '')
    mail_password: str = os.getenv('MAIL_PASSWORD', '')
    mail_from: str = os.getenv('MAIL_FROM', '')
    imap_host: str = os.getenv('IMAP_HOST', '')
    imap_port: int = int(os.getenv('IMAP_PORT', '993'))
    imap_folder: str = os.getenv('IMAP_FOLDER', 'INBOX')
    auto_sync_seconds: int = int(os.getenv('AUTO_SYNC_SECONDS', '300'))
    auto_queue_email: bool = os.getenv('AUTO_QUEUE_EMAIL', 'false').lower() == 'true'
    source_max_age_seconds: int = int(os.getenv('SOURCE_MAX_AGE_SECONDS', '900'))

    def __post_init__(self):
        if not self.demo:
            if not self.database_url.startswith('db2+ibm_db://'):
                raise ValueError('Live mode requires a Db2 DATABASE_URL.')
            if not self.oidc_tenant or not self.oidc_audience:
                raise ValueError('Live mode requires Entra tenant and audience.')
            if not self.public_url.startswith('https://') or not self.oidc_secret:
                raise ValueError('Live mode requires HTTPS PUBLIC_URL and Entra client secret.')
            for endpoint in (self.sn_url, self.jira_url, self.github_url, self.jenkins_url):
                if endpoint and not endpoint.startswith('https://'):
                    raise ValueError('Live integration endpoints must use HTTPS.')
        if self.external_writes and self.demo:
            raise ValueError('External writes require DEMO_MODE=false.')


ENVIRONMENTS = ['DEV', 'INT', 'QAT', 'PROD']
