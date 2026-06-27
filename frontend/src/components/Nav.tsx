import { NavLink } from 'react-router-dom';

const NAV_ITEMS = [
  { to: '/', label: 'Overview', icon: '◉', end: true },
  { to: '/wallets', label: 'Wallets', icon: '◈' },
  { to: '/radar', label: 'Radar', icon: '◎' },
  { to: '/signals', label: 'Signals', icon: '◇' },
  { to: '/orders', label: 'Orders', icon: '◈' },
  { to: '/portfolio', label: 'Portfolio', icon: '▤' },
  { to: '/risk', label: 'Risk', icon: '⚠' },
  { to: '/experiments', label: 'Experiments', icon: '⚗' },
  { to: '/settings', label: 'Settings', icon: '⚙' },
];

export function Nav() {
  return (
    <nav className="nav" role="navigation" aria-label="Main navigation">
      {NAV_ITEMS.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          end={item.end}
          className={({ isActive }) =>
            `nav__item ${isActive ? 'nav__item--active' : ''}`
          }
        >
          <span className="nav__icon" aria-hidden="true">{item.icon}</span>
          <span>{item.label}</span>
        </NavLink>
      ))}
    </nav>
  );
}
