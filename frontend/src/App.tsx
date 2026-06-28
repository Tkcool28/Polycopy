import { Routes, Route, Navigate } from 'react-router-dom';
import { Layout } from './components/Layout';
import { OverviewPage } from './pages/OverviewPage';
import { WalletsPage } from './pages/WalletsPage';
import { TradeRadarPage } from './pages/TradeRadarPage';
import { TradeDetailPage } from './pages/TradeDetailPage';
import { PortfolioPage } from './pages/PortfolioPage';
import { SignalsPage } from './pages/SignalsPage';
import { SettingsPage } from './pages/SettingsPage';
import { PaperOrdersPage } from './pages/PaperOrdersPage';
import { RiskConsolePage } from './pages/RiskConsolePage';
import { ExperimentsPage } from './pages/ExperimentsPage';
import { DataHealthPage } from './pages/DataHealthPage';

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
        <Route path="/orders" element={<PaperOrdersPage />} />
        <Route path="/risk" element={<RiskConsolePage />} />
        <Route path="/signals" element={<SignalsPage />} />
        <Route path="/experiments" element={<ExperimentsPage />} />
        <Route path="/data-health" element={<DataHealthPage />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
