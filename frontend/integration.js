'use strict';

(() => {
  let csrf = '';
  const panel = document.createElement('section');
  panel.id = 'session-login';
  panel.dir = 'rtl';
  panel.style.cssText = 'position:fixed;inset:0;z-index:3000;background:#edf2ef;display:grid;place-items:center;padding:20px;overflow:auto';
  panel.innerHTML = `<form style="width:min(440px,100%);background:white;padding:28px;border-radius:16px;box-shadow:0 10px 40px #1232">
    <h1>تسجيل الدخول إلى دليل</h1><p>تابع تحليلاتك وراجع المعاملات والقيود المحاسبية في مساحة عملك.</p>
    <label class="field"><span>اسم المستخدم</span><input name="username" autocomplete="username" required maxlength="80"></label>
    <label class="field"><span>كلمة المرور</span><input name="password" type="password" autocomplete="current-password" required maxlength="1024"></label>
    <p id="session-error" role="alert" style="white-space:pre-wrap"></p>
    <button class="button primary" type="submit">دخول</button>
    <p class="dialog-note">للحصول على حساب أو مساعدة في تسجيل الدخول، تواصل مع مسؤول مساحة العمل.</p>
  </form>`;
  document.body.append(panel);
  const logout = document.createElement('button');
  logout.className = 'button small'; logout.textContent = 'خروج'; logout.hidden = true;
  document.querySelector('.topbar-actions').append(logout);

  function showLogin(message = '') {
    panel.style.display = 'grid'; logout.hidden = true;
    document.querySelector('#session-error').textContent = message;
    window.Daleel.disconnect();
  }

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.method && !['GET','HEAD'].includes(options.method)) headers.set('X-CSRF-Token', csrf);
    const response = await fetch(path, {...options, headers, credentials: 'same-origin'});
    if (!(response.headers.get('content-type') || '').includes('application/json')) {
      throw new Error('استجابة غير متوقعة من الخادم (HTTP ' + response.status + '). تحقق من حجم الملفات.');
    }
    const data = await response.json();
    if (!response.ok) {
      if (response.status === 401 && path !== '/auth/login') showLogin('انتهت الجلسة. سجّل الدخول مجدداً.');
      throw new Error(data.error?.message || 'تعذّر تنفيذ الطلب');
    }
    return data;
  }

  const adapter = {
    loadWorkspace: ({periodDays}) => api('/workspace?periodDays=' + periodDays),
    async uploadFile({file, files, kind, sector}) {
      const body = new FormData();
      if (kind === 'ledger') {
        for (const key of ['headers','lines','chart_of_accounts']) body.append(key, files[key]);
        body.append('sector', sector);
      } else body.append('file', file);
      return api('/analyses/' + kind + '/upload', {method: 'POST', body});
    }
  };

  async function connect() {
    panel.style.display = 'none'; logout.hidden = false;
    await window.Daleel.connect(adapter);
  }

  panel.querySelector('form').addEventListener('submit', async event => {
    event.preventDefault(); const button = panel.querySelector('button'); button.disabled = true;
    document.querySelector('#session-error').textContent = '';
    try {
      const preflight = await api('/auth/session'); csrf = preflight.csrf_token;
      const form = new FormData(event.target);
      const result = await api('/auth/login', {method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({username:form.get('username'),password:form.get('password')})});
      csrf = result.csrf_token; event.target.elements.password.value = ''; await connect();
    } catch (error) { document.querySelector('#session-error').textContent = error.message; }
    finally { button.disabled = false; }
  });

  logout.addEventListener('click', async () => {
    try { await api('/auth/logout', {method:'POST'}); showLogin(); }
    catch(error) { showLogin(error.message); }
  });

  api('/auth/session').then(data => {
    csrf = data.csrf_token;
    if(data.authenticated) return connect();
    showLogin();
  }).catch(error => showLogin(error.message));
})();
