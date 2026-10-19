import { useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { Api, auth } from '../api';

export default function Login() {
  const navigate = useNavigate();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [adminToken, setAdminToken] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function signIn(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = await Api.login(email, password);
      auth.save(result.access_token, result.tenant_id);
      navigate('/');
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function signInAsArix(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      auth.saveAdmin(adminToken);
      const tenants = await Api.tenants();
      if (tenants.length === 0) {
        setError('Тенантов пока нет — создайте через API или make seed');
        auth.clear();
        return;
      }
      auth.save('', tenants[0].id);
      navigate('/');
    } catch (err) {
      auth.clear();
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="center">
      <div className="narrow">
        <h1>ARIX</h1>
        <p className="muted" style={{ marginTop: 0 }}>
          Панель клиники: диалоги, календарь, прайс, отчёты.
        </p>

        <form className="card" onSubmit={signIn}>
          <h2>Вход для сотрудника</h2>
          <div className="field">
            <label htmlFor="email">E-mail</label>
            <input
              id="email"
              type="email"
              autoComplete="username"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              required
            />
          </div>
          <div className="field">
            <label htmlFor="password">Пароль</label>
            <input
              id="password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
            />
          </div>
          <button className="primary" type="submit" disabled={busy}>
            Войти
          </button>
        </form>

        <form className="card" onSubmit={signInAsArix}>
          <h2>Вход ARIX (суперадмин)</h2>
          <div className="field">
            <label htmlFor="admin">ADMIN_API_TOKEN</label>
            <input
              id="admin"
              type="password"
              value={adminToken}
              onChange={(event) => setAdminToken(event.target.value)}
              required
            />
          </div>
          <button type="submit" disabled={busy}>
            Войти как ARIX
          </button>
        </form>

        {error && <p className="error">{error}</p>}
      </div>
    </div>
  );
}
