import { Outlet } from 'react-router-dom';
import { Header } from './Header';
import { Nav } from './Nav';
import { Banners } from './Banners';

export function Layout() {
  return (
    <div className="app">
      <Banners />
      <Header />
      <Nav />
      <main className="main-content">
        <Outlet />
      </main>
    </div>
  );
}
