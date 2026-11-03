/* Resolve a UI asset from the organised app/ tree.
 * HTML -> app/templates, CSS -> app/static/css, JS -> app/static/js,
 * everything else -> app/static. Replaces the old flat web/ directory. */
const path = require('path');

const APP = path.join(__dirname, '..', 'app');

function assetPath(name) {
  const base = path.basename(name);
  if (/\.html$/.test(base)) return path.join(APP, 'templates', base);
  if (/\.css$/.test(base)) {
    return path.join(APP, 'static', 'css', base === 'admin.css' ? 'tailwind.css' : base);
  }
  if (/\.js$/.test(base)) return path.join(APP, 'static', 'js', base);
  return path.join(APP, 'static', base);
}

module.exports = { APP, assetPath };
