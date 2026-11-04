document.querySelector('#login').addEventListener('submit', async event => {
  event.preventDefault();
  const button = event.target.querySelector('button');
  const error = document.querySelector('#error');
  const label = button.textContent;
  button.disabled = true;
  button.textContent = 'Signing in…';
  error.textContent = '';
  try {
    const response = await fetch('/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
      body: JSON.stringify(Object.fromEntries(new FormData(event.target))),
    });
    const contentType = response.headers.get('content-type') || '';
    if (!contentType.includes('application/json')) {
      if (response.redirected && new URL(response.url).pathname === '/admin') {
        location = '/admin';
        return;
      }
      throw Error(`Login endpoint returned ${response.status} ${contentType || 'without JSON'}. Check proxy and API logs.`);
    }
    const data = await response.json();
    if (!response.ok) throw Error(data.error || `Login failed (${response.status})`);
    location = '/admin';
  } catch (exception) {
    error.textContent = exception.message || 'Unable to contact the login service';
    button.disabled = false;
    button.textContent = label;
  }
});
