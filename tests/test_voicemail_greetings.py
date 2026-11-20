import io
import wave

import pytest

from app.voicemail import VoicemailStore
from test_api import app_client


def greeting_audio(rate=8000, seconds=1):
    output = io.BytesIO()
    with wave.open(output, 'wb') as audio:
        audio.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        audio.writeframes(b'\0\0' * int(rate * seconds))
    return output.getvalue()


def test_personal_greeting_and_automated_fallback_preserve_messages(tmp_path):
    store = VoicemailStore(str(tmp_path))
    mailbox = '101-13025550101'
    assert store.greeting_path(mailbox) is None
    store.save_greeting(mailbox, greeting_audio())
    path = store.greeting_path(mailbox)
    assert path.name == 'unavail.wav'
    assert path.stat().st_mode & 0o777 == 0o660
    (path.parent / 'temp.gsm').write_bytes(b'temporary')
    (path.parent / 'greet.wav').write_bytes(b'recorded name')
    message = path.parent / 'INBOX/msg0000.wav'
    message.write_bytes(b'message')
    store.save_greeting(mailbox, greeting_audio())
    assert not (path.parent / 'temp.gsm').exists()
    store.reset_greeting(mailbox)
    assert store.greeting_path(mailbox) is None
    assert not (path.parent / 'greet.wav').exists()
    assert message.read_bytes() == b'message'


@pytest.mark.parametrize('data', [b'not audio', greeting_audio(16000), greeting_audio(seconds=.01), greeting_audio()[:-10], b'x' * 2_000_001])
def test_invalid_greeting_does_not_replace_existing(tmp_path, data):
    store = VoicemailStore(str(tmp_path))
    store.save_greeting('101', greeting_audio())
    previous = store.greeting_path('101').read_bytes()
    with pytest.raises(ValueError):
        store.save_greeting('101', data)
    assert store.greeting_path('101').read_bytes() == previous


def test_greeting_refuses_symlink_escape(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    store = VoicemailStore(str(tmp_path / 'voice'))
    (store.root / store.context).mkdir()
    (store.root / store.context / '101').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        store.save_greeting('101', greeting_audio())
    with pytest.raises(ValueError):
        store.reset_greeting('101')
    assert list(outside.iterdir()) == []


def test_greeting_api_authorization_csrf_upload_preview_reset(tmp_path):
    client = app_client(tmp_path)
    store = client.application.extensions['settings_store']
    owner = store.save_user({'username': 'greeting-owner', 'email': 'owner@example.com', 'password': 'secure-greeting-pass-123', 'role': 'user'})
    other = store.save_user({'username': 'greeting-other', 'email': 'other@example.com', 'password': 'secure-greeting-pass-456', 'role': 'user'})
    store.save_extension({'extension': '201', 'sip_password': 'secure-sip-123'}, owner)
    store.save_extension({'extension': '202', 'sip_password': 'secure-sip-456'}, other)
    key = store.list_extensions(owner)[0]['key']
    foreign = store.list_extensions(other)[0]['key']
    base = f'/admin/api/voicemail-greetings/{key}'
    assert client.get(base).status_code == 401
    assert client.post('/admin/login', json={'username': 'greeting-owner', 'email': 'owner@example.com', 'password': 'secure-greeting-pass-123'}).status_code == 200
    csrf = {'X-CSRF-Token': client.get('/admin/api/state').json['csrf_token']}
    assert client.get(base).json['custom'] is False
    assert client.post(base).status_code == 403
    assert client.post(base, headers=csrf, data={'greeting': (io.BytesIO(greeting_audio()), 'voice.wav')}).status_code == 200
    assert client.get(base).json['custom'] is True
    assert client.get(base + '/file').status_code == 200
    for method in ('get', 'post', 'delete'):
        assert getattr(client, method)(f'/admin/api/voicemail-greetings/{foreign}', headers=csrf).status_code == 404
    assert client.get(f'/admin/api/voicemail-greetings/{foreign}/file').status_code == 404
    assert client.delete(base, headers=csrf).json['custom'] is False
    assert client.get(base + '/file').status_code == 404
