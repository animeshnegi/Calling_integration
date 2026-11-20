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


@pytest.mark.parametrize('seconds', [.1, 120])
def test_greeting_duration_boundaries_are_accepted(tmp_path, seconds):
    store = VoicemailStore(str(tmp_path))
    store.save_greeting('101', greeting_audio(seconds=seconds))
    with wave.open(str(store.greeting_path('101')), 'rb') as audio:
        assert audio.getparams()[:3] == (1, 2, 8000)
        assert audio.getnframes() == int(seconds * 8000)


def test_phone_temporary_greeting_has_priority_until_reset(tmp_path):
    store = VoicemailStore(str(tmp_path))
    store.save_greeting('101', greeting_audio())
    directory = store.greeting_path('101').parent
    (directory / 'temp.wav').write_bytes(greeting_audio(seconds=2))
    assert store.greeting_path('101').name == 'temp.wav'
    store.reset_greeting('101')
    assert store.greeting_path('101') is None


def test_same_digits_on_different_numbers_have_independent_greetings(tmp_path):
    client = app_client(tmp_path)
    settings = client.application.extensions['settings_store']
    owner = settings.save_user({'username': 'two-greetings', 'email': 'two@example.com',
                               'password': 'secure-greeting-pass-123', 'role': 'user'})
    numbers = ('+13025550041', '+13025550042')
    for number in numbers:
        settings.save_number({'number': number, 'provider': 'TestProvider', 'owner_user_id': owner, 'active': True})
        settings.add_extension_to_number(number, owner, {'extension': '101', 'voicemail_enabled': True, 'voicemail_pin': '4321'})
    client.post('/admin/login', json={'username': 'two-greetings', 'password': 'secure-greeting-pass-123'})
    headers = {'X-CSRF-Token': client.get('/admin/api/state').json['csrf_token']}
    first, second = [f'/admin/api/voicemail-greetings/101@{number}' for number in numbers]
    # Digits alone never guess a tenant's number-scoped mailbox.
    assert client.get('/admin/api/voicemail-greetings/101').status_code == 404
    for url, seconds in ((first, 1), (second, 2)):
        assert client.post(url, headers=headers, data={'greeting': (io.BytesIO(greeting_audio(seconds=seconds)), 'voice.wav')}).status_code == 200
    assert client.get(first + '/file').data != client.get(second + '/file').data
    assert client.post(first, headers=headers, data={'greeting': (io.BytesIO(b'broken'), 'bad.wav')}).status_code == 400
    assert client.get(first).json['custom'] is True
    # Admin access uses the same scoped files, not a separate copy.
    admin = client.application.test_client()
    admin.post('/admin/login', json={'username': 'admin', 'password': 'test-admin-password-1234'})
    assert admin.get(first + '/file').data == client.get(first + '/file').data
    assert client.delete(first, headers=headers).json['custom'] is False
    assert client.get(second).json['custom'] is True
    assert client.get(second + '/file').status_code == 200


def test_browser_wav_encoder_produces_valid_asterisk_audio():
    """Exercise the JS WAV writer with simulated Web Audio, not a real browser/decoder."""
    import base64
    import json
    from pathlib import Path
    import shutil
    import subprocess

    node = shutil.which('node')
    if not node:
        pytest.skip('node is required to exercise browser WAV encoding')
    source = (Path(__file__).resolve().parents[1] / 'web/admin.js').read_text()
    function = source.split('async function greetingWav(file) {', 1)[1].split('async function changeGreeting', 1)[0]
    script = '''
let closed = 0;
global.AudioContext = class {
  async decodeAudioData() { return {duration: 0.1}; }
  async close() { closed++; }
};
global.OfflineAudioContext = class {
  constructor(channels, frames, rate) {
    if (channels !== 1 || frames !== 800 || rate !== 8000) throw Error('incorrect rendering format');
    this.destination = {};
  }
  createBufferSource() { return {connect() {}, start() {}}; }
  async startRendering() { return {getChannelData() {
    const data = new Float32Array(800); data.set([-2, -1, 0, 0.5, 1, 2]); return data;
  }}; }
};
''' + 'async function greetingWav(file) {' + function + '''
(async () => {
  const result = await greetingWav({size: 1, async arrayBuffer() { return new ArrayBuffer(1); }});
  console.log(JSON.stringify({closed, audio: Buffer.from(await result.arrayBuffer()).toString('base64')}));
})().catch(error => { console.error(error); process.exit(1); });
'''
    result = subprocess.run([node, '-e', script], check=True, text=True, capture_output=True, timeout=10)
    data = json.loads(result.stdout)
    assert data['closed'] == 1
    import struct
    with wave.open(io.BytesIO(base64.b64decode(data['audio'])), 'rb') as audio:
        assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getnframes()) == (1, 2, 8000, 800)
        assert struct.unpack('<6h', audio.readframes(6)) == (-32768, -32768, 0, 16383, 32767, 32767)
