/* Applies the remembered skin before the first paint so a reload never
       flashes the other theme. The role decides the default on a first visit,
       once the state payload names it. */
    (function () {
      try {
        var role = localStorage.getItem('eip-console-role');
        var skin = localStorage.getItem('eip-console-theme' + (role ? ':' + role : ''));
        if (skin === 'light' || skin === 'dark') document.body.classList.add('theme-' + skin);
      } catch (e) { /* storage unavailable: keep the role default */ }
    })();
