import { Routes, Route, Navigate } from 'react-router-dom';
import { Layout } from './components/Layout';
import { OverviewPage } from './pages/OverviewPage';
import { WalletsPage } from './pages/WalletsPage';
import { TradeRadarPage } from './pages/TradeRadarPage';
import { TradeDetailPage } from './pages/TradeDetailPage';
import { PortfolioPage } from './pages/PortfolioPage';
import { SignalsPage } from './pages/SignalsPage';
import { SettingsPage } from './pages/SettingsPage';

export function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<OverviewPage />} />
        <Route path="/wallets" element={<WalletsPage />} />
        <Route path="/wallets/:id" element={<WalletsPage />} />
        <Route path="/radar" element={<TradeRadarPage />} />
        <Route path="/radar/:signalId" element={<TradeDetailPage />} />
        <Route path="/portfolio" element={<PortfolioPage />} />
        <Route path="/signals" element={<SignalsPage />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
