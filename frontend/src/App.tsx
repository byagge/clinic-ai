import { NavLink, Navigate, Route, Routes, useNavigate } from 'react-router-dom';

import { auth } from './api';
import Calendar from './pages/Calendar';
import Channels from './pages/Channels';
import Dashboard from './pages/Dashboard';
import Dialogs from './pages/Dialogs';
import Knowledge from './pages/Knowledge';
import Login from './pages/Login';
import MiniApp from './pages/MiniApp';
import Services from './pages/Services';

const NAV = [
  ['/', 'Сводка'],
  ['/calendar', 'Календарь'],
  ['/dialogs', 'Диалоги'],
  ['/services', 'Прайс'],
  ['/kb', 'База знаний'],
  ['/channels', 'Каналы'],
] as const;

function Shell({ children }: { children: React.ReactNode }) {
  const navigate = useNavigate();
  if (!auth.isAuthenticated || !auth.tenantId) return <Navigate to="/login" replace />;
  return (
    <div className="layout">
      <nav className="sidebar">
        <div className="brand">
          ARIX
          <small>ИИ-менеджер клиники</small>
        </div>
        {NAV.map(([path, label]) => (
          <NavLink
            key={path}
            to={path}
            end={path === '/'}
            className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}
          >
            {label}
          </NavLink>
        ))}
        <div className="sidebar-footer">
          <button
            className="small"
            onClick={() => {
              auth.clear();
              navigate('/login');
            }}
          >
            Выйти
          </button>
        </div>
      </nav>
      <main className="content">{children}</main>
    </div>
  );
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/app/:slug" element={<MiniApp />} />
      <Route
        path="*"
        element={
          <Shell>
            <Routes>
              <Route path="/" element={<Dashboard />} />
              <Route path="/calendar" element={<Calendar />} />
              <Route path="/dialogs" element={<Dialogs />} />
              <Route path="/services" element={<Services />} />
              <Route path="/kb" element={<Knowledge />} />
              <Route path="/channels" element={<Channels />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </Shell>
        }
      />
    </Routes>
  );
}
