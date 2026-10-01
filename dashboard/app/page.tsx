'use client';

import { useEffect, useState } from 'react';
import { Activity, BarChart3, Bell, BriefcaseBusiness, CheckCircle2, ChevronRight, CircleGauge, Clock3, Database, FileClock, LineChart, Menu, RefreshCw, Search, Settings2, ShieldCheck, Sparkles, Target, TrendingUp, WalletCards, X } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { ChartContainer, ChartTooltip, ChartTooltipContent } from '@/components/ui/chart';
import { CartesianGrid, Line, LineChart as PerformanceLineChart, XAxis, YAxis } from 'recharts';

const positions = [
  { symbol: 'NVDA', side: 'LONG', weight: 9.8, signal: 0.91, price: 184.72, move: 2.34 },
  { symbol: 'META', side: 'LONG', weight: 9.1, signal: 0.86, price: 812.16, move: 1.12 },
  { symbol: 'AVGO', side: 'LONG', weight: 8.7, signal: 0.82, price: 387.91, move: 0.88 },
  { symbol: 'ADBE', side: 'SHORT', weight: -8.2, signal: -0.78, price: 338.44, move: -1.45 },
  { symbol: 'UNH', side: 'SHORT', weight: -9.0, signal: -0.84, price: 287.63, move: -0.72 },
  { symbol: 'PEP', side: 'SHORT', weight: -9.6, signal: -0.89, price: 148.06, move: -0.36 },
];
const backtestCurve = [
  { label: 'Sep 20', strategy: 2200, spy: 2288 }, { label: 'Dec 20', strategy: 2200, spy: 2565 },
  { label: 'Mar 21', strategy: 2198, spy: 2727 }, { label: 'Jun 21', strategy: 2188, spy: 2957 },
  { label: 'Sep 21', strategy: 2091, spy: 2973 }, { label: 'Dec 21', strategy: 2094, spy: 3302 },
  { label: 'Mar 22', strategy: 2111, spy: 3151 }, { label: 'Jun 22', strategy: 2264, spy: 2643 },
  { label: 'Sep 22', strategy: 2247, spy: 2513 }, { label: 'Dec 22', strategy: 2260, spy: 2703 },
  { label: 'Mar 23', strategy: 2064, spy: 2904 }, { label: 'Jun 23', strategy: 2174, spy: 3156 },
  { label: 'Sep 23', strategy: 2133, spy: 3053 }, { label: 'Dec 23', strategy: 2201, spy: 3410 },
  { label: 'Mar 24', strategy: 2275, spy: 3764 }, { label: 'Jun 24', strategy: 2262, spy: 3928 },
  { label: 'Sep 24', strategy: 2190, spy: 4157 }, { label: 'Dec 24', strategy: 2351, spy: 4258 },
  { label: 'Mar 25', strategy: 2330, spy: 4076 }, { label: 'Jun 25', strategy: 2343, spy: 4516 },
  { label: 'Sep 25', strategy: 2334, spy: 4882 }, { label: 'Dec 25', strategy: 2409, spy: 5012 },
  { label: 'Mar 26', strategy: 2515, spy: 4792 }, { label: 'Jun 26', strategy: 2474, spy: 5518 },
  { label: 'Aug 26', strategy: 2362, spy: 5685 },
];
const backtestChartConfig = { strategy: { label: 'Northstar', color: '#73f0b5' }, spy: { label: 'SPY', color: '#8da2c0' } };

type BacktestMetrics = {
  strategy: { ending_equity: number; total_return: number; cagr: number; annual_volatility: number; sharpe_zero_rf: number; max_drawdown: number };
  benchmark: { ending_equity: number; total_return: number; cagr: number; annual_volatility: number; sharpe_zero_rf: number; max_drawdown: number };
  comparison: { beta: number; correlation: number; information_ratio: number };
  assumptions: { start: string; end: string; initial_capital: number; transaction_cost_bps: number };
  average_monthly_turnover: number;
};

type AlpacaSnapshot = {
  as_of: string;
  account_mode: 'PAPER' | 'LIVE';
  equity: number;
  cash: number;
  last_equity: number;
  day_pl: number;
  gross_exposure: number;
  positions: Array<{ symbol: string; side: 'LONG' | 'SHORT'; quantity: number; market_value: number; weight: number; price: number; day_change: number; unrealized_pl: number; unrealized_plpc: number }>;
};

const currency = (value: number) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(value);
const signedMoney = (value: number) => `${value >= 0 ? '+' : '−'}${currency(Math.abs(value))}`;
const percent = (value: number) => `${value >= 0 ? '+' : '−'}${Math.abs(value * 100).toFixed(2)}%`;
const plainPercent = (value: number) => `${Math.abs(value * 100).toFixed(1)}%`;
const points = (value: number) => `${value >= 0 ? '+' : '−'}${Math.abs(value * 100).toFixed(1)} pts`;
const decimal = (value: number) => value.toFixed(2);

function parseCurve(csv: string) {
  const lines = csv.trim().split('\n').slice(1);
  const parsed = lines.map((line) => {
    const [date, strategy, benchmark] = line.split(',');
    return { date: new Date(date), strategy: Number(strategy), spy: Number(benchmark) };
  }).filter((row) => Number.isFinite(row.strategy) && Number.isFinite(row.spy));
  return parsed.filter((row, index) => index === parsed.length - 1 || row.date.getUTCMonth() % 3 === 2).map((row) => ({
    ...row,
    label: row.date.toLocaleDateString('en-US', { month: 'short', year: '2-digit', timeZone: 'UTC' }),
  }));
}

export default function Home() {
  const [mobileNav, setMobileNav] = useState(false);
  const [refreshed, setRefreshed] = useState(false);
  const [rebalanceOpen, setRebalanceOpen] = useState(false);
  const [rulesOpen, setRulesOpen] = useState(false);
  const [queued, setQueued] = useState(false);
  const [metrics, setMetrics] = useState<BacktestMetrics | null>(null);
  const [liveCurve, setLiveCurve] = useState(backtestCurve);
  const [snapshot, setSnapshot] = useState<AlpacaSnapshot | null>(null);
  useEffect(() => {
    Promise.all([fetch('/backtest_metrics.json'), fetch('/backtest_curve.csv')])
      .then(async ([metricsResponse, curveResponse]) => {
        if (!metricsResponse.ok || !curveResponse.ok) throw new Error('Backtest data is unavailable');
        return [await metricsResponse.json() as BacktestMetrics, parseCurve(await curveResponse.text())] as const;
      })
      .then(([nextMetrics, nextCurve]) => {
        setMetrics(nextMetrics);
        if (nextCurve.length) setLiveCurve(nextCurve);
      })
      .catch(() => undefined);
  }, []);
  const refreshSnapshot = async () => {
    setRefreshed(true);
    try {
      const response = await fetch(`/alpaca_snapshot.json?at=${Date.now()}`, { cache: 'no-store' });
      if (!response.ok) throw new Error('Account snapshot is unavailable');
      setSnapshot(await response.json() as AlpacaSnapshot);
    } catch {
      setSnapshot(null);
    } finally {
      setTimeout(() => setRefreshed(false), 700);
    }
  };
  useEffect(() => {
    void refreshSnapshot();
    const timer = setInterval(() => { void refreshSnapshot(); }, 60_000);
    return () => clearInterval(timer);
  }, []);
  const backtest = metrics ?? {
    strategy: { ending_equity: 2362, total_return: 0.074, cagr: 0.012, annual_volatility: 0.086, sharpe_zero_rf: 0.18, max_drawdown: -0.136 },
    benchmark: { ending_equity: 5685, total_return: 1.584, cagr: 0.169, annual_volatility: 0.165, sharpe_zero_rf: 1.03, max_drawdown: -0.245 },
    comparison: { beta: 0, correlation: -0.01, information_ratio: -0.83 },
    assumptions: { start: '2020-07-27', end: '2026-08-28', initial_capital: 2200, transaction_cost_bps: 10 },
    average_monthly_turnover: 0.57,
  } satisfies BacktestMetrics;
  const returnGap = backtest.strategy.total_return - backtest.benchmark.total_return;
  const displayedPositions = snapshot ? snapshot.positions.map((position) => ({
    symbol: position.symbol,
    side: position.side,
    weight: position.weight * (position.side === 'LONG' ? 100 : -100),
    signal: position.unrealized_plpc,
    profit: position.unrealized_pl,
    price: position.price,
    move: position.day_change * 100,
  })) : [];
  const openProfit = snapshot?.positions.reduce((sum, p) => sum + p.unrealized_pl, 0) ?? 0;
  const accountMode = snapshot?.account_mode ?? 'PREVIEW';
  const accountTimestamp = snapshot ? new Date(snapshot.as_of).toLocaleString('en-US', { timeZone: 'America/New_York', timeStyle: 'short', dateStyle: 'medium' }) : 'Awaiting paper sync';
  const longExposure = snapshot ? snapshot.positions.filter((position) => position.side === 'LONG').reduce((total, position) => total + Math.abs(position.market_value), 0) / snapshot.equity : 0.30;
  const shortExposure = snapshot ? snapshot.positions.filter((position) => position.side === 'SHORT').reduce((total, position) => total + Math.abs(position.market_value), 0) / snapshot.equity : 0.30;
  const cashWeight = snapshot ? Math.max(0, snapshot.cash / snapshot.equity) : 0.40;
  return <main className="min-h-screen bg-background text-foreground">
    <aside className={`fixed inset-y-0 left-0 z-40 w-64 border-r border-white/7 bg-[#0b1018]/96 p-4 backdrop-blur-xl transition-transform lg:translate-x-0 ${mobileNav ? 'translate-x-0' : '-translate-x-full'}`}>
      <div className="flex h-12 items-center justify-between px-2"><div className="flex items-center gap-2.5"><div className="grid size-8 place-items-center rounded-lg bg-[#73f0b5] text-[#07110d]"><LineChart className="size-4" /></div><div><p className="text-sm font-semibold tracking-tight">Northstar LS</p><p className="text-[10px] uppercase tracking-[.18em] text-slate-500">Alpaca workspace</p></div></div><button className="lg:hidden" aria-label="Close navigation" onClick={() => setMobileNav(false)}><X className="size-4" /></button></div>
      <nav className="mt-7 space-y-1" aria-label="Primary"><NavItem icon={CircleGauge} label="Overview" active /><NavItem icon={BriefcaseBusiness} label="Portfolio" /><NavItem icon={Sparkles} label="Signals" /><NavItem icon={BarChart3} label="Analytics" /><NavItem icon={FileClock} label="Order history" /></nav>
      <p className="mb-2 mt-8 px-3 text-[10px] font-semibold uppercase tracking-[.18em] text-slate-600">System</p>
      <nav className="space-y-1" aria-label="System"><NavItem icon={Database} label="Data feeds" /><NavItem icon={ShieldCheck} label="Risk controls" /><NavItem icon={Settings2} label="Settings" /></nav>
      <div className="absolute inset-x-4 bottom-4 rounded-xl border border-[#f4c667]/15 bg-[#f4c667]/5 p-3.5"><div className="mb-3 flex items-center justify-between"><span className="text-xs font-medium">Alpaca account data</span><span className={`size-2 rounded-full shadow-[0_0_12px] ${snapshot ? 'bg-[#73f0b5] shadow-[#73f0b5]' : 'bg-[#f4c667] shadow-[#f4c667]'}`} /></div><div className="flex items-center gap-2"><Badge className={snapshot?.account_mode === 'LIVE' ? 'bg-[#ff718b]/12 text-[#ff8b9e]' : 'bg-[#f4c667]/12 text-[#f4c667]'}>{accountMode}</Badge><span className="text-[11px] text-slate-500">{snapshot ? 'Snapshot loaded' : 'Awaiting sync'}</span></div></div>
    </aside>
    <div className="lg:pl-64">
      <header className="sticky top-0 z-30 flex h-16 items-center gap-3 border-b border-white/7 bg-background/85 px-4 backdrop-blur-xl sm:px-6 lg:px-8"><button className="grid size-9 place-items-center rounded-lg border border-white/8 lg:hidden" aria-label="Open navigation" onClick={() => setMobileNav(true)}><Menu className="size-4" /></button><div className="relative hidden max-w-sm flex-1 md:block"><Search className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-slate-600" /><input className="h-9 w-full rounded-lg border border-white/7 bg-white/3 pl-9 pr-12 text-xs outline-none placeholder:text-slate-600 focus:border-[#73f0b5]/40" placeholder="Search positions, signals, orders…" /><span className="absolute right-3 top-1/2 -translate-y-1/2 rounded border border-white/8 px-1.5 py-0.5 text-[9px] text-slate-600">⌘ K</span></div><div className="ml-auto flex items-center gap-2"><div className="hidden items-center gap-2 text-xs text-slate-500 sm:flex"><Clock3 className="size-3.5" />{accountTimestamp}</div><Button variant="outline" size="icon" aria-label="Notifications"><Bell className="size-4" /></Button><Button variant="outline" size="sm" onClick={refreshSnapshot}><RefreshCw className={refreshed ? 'animate-spin' : ''} />{refreshed ? 'Reloading…' : 'Reload snapshot'}</Button></div></header>
      <div className="mx-auto max-w-[1480px] p-4 sm:p-6 lg:p-8">
        <section className="mb-6 flex flex-col justify-between gap-4 md:flex-row md:items-end"><div><div className="mb-2 flex items-center gap-2 text-xs text-slate-500"><span>Strategies</span><ChevronRight className="size-3" /><span className="text-slate-300">US Equity LS</span><Badge variant="outline" className={`ml-1 ${snapshot ? 'border-[#73f0b5]/25 text-[#73f0b5]' : 'border-[#f4c667]/20 text-[#f4c667]'}`}>{snapshot ? `${accountMode} snapshot` : 'Preview data'}</Badge></div><h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">Your account at a glance</h1><p className="mt-1.5 text-sm text-slate-500">Account value, daily change, and profit on the stocks you still hold.</p></div><div className="flex gap-2"><Button variant="outline" onClick={() => setRulesOpen(true)}><Settings2 />Example rules</Button><Button className="bg-[#73f0b5] text-[#07110d] hover:bg-[#8df6c8]" onClick={() => setRebalanceOpen(true)}><Target />Example orders</Button></div></section>
        <div className="mb-4 rounded-xl border border-white/10 bg-white/[.025] p-4 text-sm text-slate-300"><strong>{snapshot ? `Saved ${accountMode.toLowerCase()} account snapshot · ${accountTimestamp} ET` : 'Account data unavailable'}</strong><p className="mt-1 text-slate-400">This screen checks for updated account data every minute. The local account sync also runs every minute. The timestamp above shows when Alpaca was last read; if it stops advancing, the sync is offline.</p></div>
        <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <Metric title="Total account value" value={snapshot ? currency(snapshot.equity) : 'Unavailable'} change={accountMode} icon={WalletCards}><p className="mt-4 text-sm text-slate-400">Cash + owned stocks − the value of borrowed stocks sold short.</p></Metric>
          <Metric title="Today’s profit / loss" value={snapshot ? signedMoney(snapshot.day_pl) : 'Unavailable'} change={snapshot && snapshot.last_equity ? percent(snapshot.day_pl / snapshot.last_equity) : '—'} icon={Activity}><p className="mt-4 text-sm text-slate-400">Change in account value since the previous closing equity, as of this snapshot.</p></Metric>
          <Metric title="Open-position profit / loss" value={snapshot ? signedMoney(openProfit) : 'Unavailable'} change="Since entry" icon={TrendingUp}><p className="mt-4 text-sm text-slate-400">Combined unrealized gain or loss on current holdings. Excludes closed trades.</p></Metric>
          <Metric title="Cash balance" value={snapshot ? currency(snapshot.cash) : 'Unavailable'} change={snapshot ? `${plainPercent(cashWeight)} of equity` : '—'} icon={WalletCards}><p className="mt-4 text-sm text-slate-400">Includes cash from short sales. This is not the same as money available to withdraw.</p></Metric>
        </section>
        <section className="mt-4 grid gap-4 xl:grid-cols-[minmax(0,1.65fr)_minmax(320px,.75fr)]">
          <Card className="border-white/7 bg-card/70"><CardHeader className="flex-row items-center justify-between"><div><CardTitle>Active positions</CardTitle><p className="mt-1 text-xs text-slate-500">Long = owned stocks; short = borrowed stocks sold. Profit is measured since entry.</p></div><Badge variant="outline" className="border-white/8 text-slate-400">{snapshot ? `${snapshot.positions.length} filled` : '6 of 10 shown'}</Badge></CardHeader><CardContent className="overflow-x-auto px-0"><table className="w-full min-w-[680px] text-left text-sm"><thead className="border-y border-white/7 bg-white/[.018] text-xs uppercase tracking-[.12em] text-slate-600"><tr><th className="px-4 py-3 font-medium">Asset</th><th className="px-4 py-3 font-medium">Position</th><th className="px-4 py-3 font-medium">Profit / loss</th><th className="px-4 py-3 font-medium">Account share</th><th className="px-4 py-3 font-medium">Snapshot price</th><th className="px-4 py-3 text-right font-medium">Stock today</th></tr></thead><tbody>{displayedPositions.map((position) => <PositionRow key={position.symbol} {...position} />)}</tbody></table></CardContent></Card>
          <Card className="border-white/7 bg-card/70"><CardHeader><CardTitle>How your money is positioned</CardTitle></CardHeader><CardContent className="space-y-4 text-sm text-slate-400">{snapshot ? <><p><strong className="text-slate-100">{plainPercent(longExposure)} long</strong> · benefits when owned stocks rise.</p><p><strong className="text-slate-100">{plainPercent(shortExposure)} short</strong> · benefits when borrowed stocks fall.</p><p><strong className="text-slate-100">{plainPercent(snapshot.gross_exposure)} total exposure</strong> · long + short position sizes, divided by account value.</p><p><strong className="text-slate-100">{percent(longExposure - shortExposure)} net exposure</strong> · long minus short. {longExposure > shortExposure ? 'Your holdings lean toward rising prices.' : longExposure < shortExposure ? 'Your holdings lean toward falling prices.' : 'The two sides are balanced.'}</p><p className="border-t border-white/10 pt-4">Short sales create cash and a matching liability. Cash and total exposure therefore do not add up to 100%.</p></> : <p>Load an account snapshot to see exposure.</p>}<p className="border-t border-white/10 pt-4">Live volatility, market coverage, and the next rebalance are not verified by this dashboard. Example strategy rules and orders are available above.</p></CardContent></Card>
        </section>
        <details className="mt-6 rounded-xl border border-white/10 p-4"><summary className="cursor-pointer text-lg font-semibold">Historical strategy tests <span className="ml-2 text-sm font-normal text-slate-400">Simulations, not your account returns</span></summary><p className="my-4 text-sm leading-6 text-slate-400">These tests ask what a hypothetical investment would have earned in the past. SPY is an S&amp;P 500 fund used for comparison. Test periods can differ between panels.</p><div className="mb-4 grid gap-3 text-sm text-slate-400 sm:grid-cols-2"><p><strong className="text-slate-200">Total return:</strong> gain over the full test. <strong className="text-slate-200">Annualized:</strong> equivalent compounded gain per year.</p><p><strong className="text-slate-200">Max drawdown:</strong> largest peak-to-trough loss. <strong className="text-slate-200">Sharpe:</strong> return relative to volatility.</p><p><strong className="text-slate-200">Beta / correlation:</strong> how the strategy moved with SPY. <strong className="text-slate-200">Turnover:</strong> how much was traded.</p><p><strong className="text-slate-200">Information ratio:</strong> return relative to SPY per unit of relative risk. <strong className="text-slate-200">Return gap:</strong> difference in percentage points.</p></div>
          <Card className="mb-4 border-[#ff718b]/15 bg-[#ff718b]/[.025]"><CardHeader className="flex-row items-start justify-between"><div><div className="mb-2 flex items-center gap-2"><Badge className="bg-[#ff718b]/10 text-[#ff8b9e]">SENTIMENT TEST REJECTED</Badge><span className="text-[11px] text-slate-600">5,329 timestamped Alpaca/Benzinga news events</span></div><CardTitle className="text-lg">News overlay did not improve performance</CardTitle><p className="mt-1 text-xs text-slate-500">Point-in-time news · publication-time filtering · three-day decay · no future information</p></div><Database className="size-5 text-[#ff718b]" /></CardHeader><CardContent><div className="grid gap-3 sm:grid-cols-3"><RelativeMetric label="News-enhanced" value="$4,935" note="+124.3% total return" /><RelativeMetric label="Price-only 90/10" value="$4,995" note="+127.0% total return" /><RelativeMetric label="SPY" value="$5,685" note="+158.4% total return" /></div><p className="mt-4 border-t border-white/6 pt-4 text-xs leading-5 text-slate-500">The headline overlay reduced ending value by $60 and produced negative estimated alpha. It remains disabled. Social sentiment was not backfilled because no documented point-in-time Stocktwits archive was available; using present-day posts in a historical test would be invalid.</p></CardContent></Card>
          <Card className="mb-4 border-[#73f0b5]/15 bg-[#73f0b5]/[.025]">
            <CardHeader className="flex-row items-start justify-between"><div><div className="mb-2 flex items-center gap-2"><Badge className="bg-[#73f0b5]/10 text-[#73f0b5]">NEW MODEL TESTED</Badge><span className="text-[11px] text-slate-600">No post-test parameter tuning</span></div><CardTitle className="text-lg">Enhanced 90/10 vs. SPY</CardTitle><p className="mt-1 text-xs text-slate-500">90% SPY core · 10% momentum/low-volatility sleeve · monthly rebalance · 10 bps costs</p></div><TrendingUp className="size-5 text-[#73f0b5]" /></CardHeader>
            <CardContent><div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5"><BacktestMetric label="Ending value" strategy="$4,995" benchmark="$5,685" /><BacktestMetric label="Total return" strategy="+127.0%" benchmark="+158.4%" /><BacktestMetric label="Annualized" strategy="+14.4%" benchmark="+16.9%" /><BacktestMetric label="Max drawdown" strategy="−23.7%" benchmark="−24.5%" positive /><BacktestMetric label="Sharpe ratio" strategy="0.94" benchmark="1.03" /></div><div className="mt-5 grid gap-4 border-t border-white/6 pt-4 lg:grid-cols-[1fr_auto]"><div><p className="text-xs font-medium text-slate-300">Result</p><p className="mt-1 max-w-3xl text-xs leading-5 text-slate-500">The 90/10 design improved the original strategy by 119.7 percentage points, but still trailed SPY by 31.4 points. Its 10% factor sleeve produced negative estimated alpha, so SPY remains the stronger baseline in this sample.</p></div><div className="flex flex-wrap items-center gap-2 lg:justify-end"><Badge variant="outline" className="border-white/8 text-slate-500">0.90 beta</Badge><Badge variant="outline" className="border-white/8 text-slate-500">10.8% turnover</Badge><Badge variant="outline" className="border-[#f4c667]/20 text-[#f4c667]">Needs out-of-sample test</Badge></div></div></CardContent>
          </Card>
          <Card className="border-white/7 bg-card/70">
            <CardHeader className="flex-row items-start justify-between"><div><div className="mb-2 flex items-center gap-2"><Badge className="bg-[#73f0b5]/10 text-[#73f0b5]">ORIGINAL MODEL</Badge><span className="text-[11px] text-slate-600">Alpaca adjusted daily bars</span></div><CardTitle className="text-lg">Original long/short vs. SPY</CardTitle><p className="mt-1 text-xs text-slate-500">{backtest.assumptions.start} – {backtest.assumptions.end} · {currency(backtest.assumptions.initial_capital)} initial · monthly rebalance · {backtest.assumptions.transaction_cost_bps} bps turnover cost</p></div><BarChart3 className="size-5 text-slate-600" /></CardHeader>
            <CardContent><div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5"><BacktestMetric label="Ending value" strategy={currency(backtest.strategy.ending_equity)} benchmark={currency(backtest.benchmark.ending_equity)} /><BacktestMetric label="Total return" strategy={percent(backtest.strategy.total_return)} benchmark={percent(backtest.benchmark.total_return)} /><BacktestMetric label="Annualized" strategy={percent(backtest.strategy.cagr)} benchmark={percent(backtest.benchmark.cagr)} /><BacktestMetric label="Max drawdown" strategy={`−${plainPercent(backtest.strategy.max_drawdown)}`} benchmark={`−${plainPercent(backtest.benchmark.max_drawdown)}`} positive /><BacktestMetric label="Sharpe ratio" strategy={decimal(backtest.strategy.sharpe_zero_rf)} benchmark={decimal(backtest.benchmark.sharpe_zero_rf)} /></div>
              <div className="mt-5 grid gap-5 border-t border-white/6 pt-5 xl:grid-cols-[minmax(0,1.6fr)_minmax(280px,.7fr)]">
                <div><div className="mb-3 flex items-center justify-between"><div><p className="text-xs font-medium text-slate-300">Growth of $2,200</p><p className="mt-1 text-[11px] text-slate-600">Quarter-end values · adjusted close</p></div><div className="flex gap-3 text-[10px]"><span className="flex items-center gap-1.5 text-slate-400"><i className="size-2 rounded-full bg-[#73f0b5]" />Northstar</span><span className="flex items-center gap-1.5 text-slate-400"><i className="size-2 rounded-full bg-[#8da2c0]" />SPY</span></div></div>
                  <ChartContainer config={backtestChartConfig} className="h-[240px] w-full aspect-auto"><PerformanceLineChart data={liveCurve} margin={{ left: 4, right: 10, top: 8, bottom: 0 }}><CartesianGrid vertical={false} stroke="rgba(255,255,255,.055)" /><XAxis dataKey="label" tickLine={false} axisLine={false} minTickGap={28} /><YAxis tickLine={false} axisLine={false} width={54} tickFormatter={(value) => `$${(value / 1000).toFixed(1)}k`} domain={[1800, 6000]} /><ChartTooltip content={<ChartTooltipContent formatter={(value, name) => <><span className="text-slate-400">{name === 'strategy' ? 'Northstar' : 'SPY'}</span><span className="ml-auto font-mono text-slate-100">${Number(value).toLocaleString()}</span></>} />} /><Line type="monotone" dataKey="strategy" stroke="var(--color-strategy)" strokeWidth={2.4} dot={false} /><Line type="monotone" dataKey="spy" stroke="var(--color-spy)" strokeWidth={2.4} dot={false} /></PerformanceLineChart></ChartContainer>
                </div>
                <div className="grid content-start gap-3 sm:grid-cols-3 xl:grid-cols-1"><RelativeMetric label="Return gap" value={points(returnGap)} note="Northstar minus SPY" warning={returnGap < 0} /><RelativeMetric label="Market beta" value={decimal(backtest.comparison.beta)} note="Matched daily returns" /><RelativeMetric label="Correlation" value={decimal(backtest.comparison.correlation)} note="Matched daily returns" /><RelativeMetric label="Information ratio" value={decimal(backtest.comparison.information_ratio)} note="Active return per unit of risk" warning={backtest.comparison.information_ratio < 0} /></div>
              </div>
              <div className="mt-5 grid gap-4 border-t border-white/6 pt-4 lg:grid-cols-[1fr_auto]"><div><p className="text-xs font-medium text-slate-300">Verdict</p><p className="mt-1 max-w-3xl text-xs leading-5 text-slate-500">The backtest is market-neutral by construction, but it trails SPY by {Math.abs(returnGap * 100).toFixed(1)} percentage points over this sample. Treat it as research only: the static universe has survivorship bias and short borrow costs are excluded.</p></div><div className="flex flex-wrap items-center gap-2 lg:justify-end"><Badge variant="outline" className="border-white/8 text-slate-500">{plainPercent(backtest.strategy.annual_volatility)} vol</Badge><Badge variant="outline" className="border-white/8 text-slate-500">{plainPercent(backtest.average_monthly_turnover)} avg turnover</Badge><Badge variant="outline" className="border-[#f4c667]/20 text-[#f4c667]">Survivorship biased</Badge></div></div></CardContent>
          </Card>
        </details>
      </div>
      {rebalanceOpen && <Modal title="Example orders — not scheduled" description="Illustrative orders based on the preview dataset. Connecting paper data will replace these values." onClose={() => setRebalanceOpen(false)} wide><div className="rounded-lg border border-white/7"><div className="grid grid-cols-[1fr_80px_100px] border-b border-white/7 px-3 py-2 text-[10px] uppercase tracking-wider text-slate-600"><span>Asset</span><span>Side</span><span className="text-right">Target</span></div>{positions.map((p) => <div key={p.symbol} className="grid grid-cols-[1fr_80px_100px] items-center border-b border-white/5 px-3 py-2.5 text-xs last:border-0"><span className="font-semibold">{p.symbol}</span><span className={p.side === 'LONG' ? 'text-[#73f0b5]' : 'text-[#ff718b]'}>{p.side === 'LONG' ? 'BUY' : 'SELL'}</span><span className="text-right font-mono">{Math.abs(p.weight).toFixed(1)}%</span></div>)}</div><div className="flex items-start gap-2 rounded-lg bg-[#f4c667]/7 p-3 text-xs text-[#e9d6a4]"><ShieldCheck className="mt-0.5 size-4 shrink-0" />No order can reach a live account from this dashboard. Paper execution remains gated in the Python strategy.</div><div className="flex justify-end gap-2"><Button variant="outline" onClick={() => setRebalanceOpen(false)}>Cancel</Button><Button className="bg-[#73f0b5] text-[#07110d]" onClick={() => { setQueued(true); setRebalanceOpen(false); }}>{queued ? <CheckCircle2 /> : <Target />}{queued ? 'Example marked reviewed' : 'Mark example reviewed'}</Button></div></Modal>}
      {rulesOpen && <Modal title="Strategy rules" description="Illustrative strategy settings, not a live risk check." onClose={() => setRulesOpen(false)}><div className="space-y-1">{[['Signal window','126 days, skip 21'],['Portfolio','5 long / 5 short'],['Max position','10.0%'],['Gross exposure','60% initial target'],['Annual volatility','12.0% target'],['Liquidity floor','$20M average daily']].map(([label,value]) => <div key={label} className="flex items-center justify-between border-b border-white/5 py-3 text-xs"><span className="text-slate-500">{label}</span><span className="font-mono text-slate-200">{value}</span></div>)}</div><div className="flex justify-end"><Button variant="outline" onClick={() => setRulesOpen(false)}>Done</Button></div></Modal>}
    </div>
  </main>;
}

function NavItem({ icon: Icon, label, active, count }: { icon: typeof Activity; label: string; active?: boolean; count?: string }) { return <button className={`flex h-10 w-full items-center gap-3 rounded-lg px-3 text-sm transition ${active ? 'bg-white/[.065] text-white' : 'text-slate-500 hover:bg-white/[.035] hover:text-slate-300'}`}><Icon className={`size-4 ${active ? 'text-[#73f0b5]' : ''}`} /><span>{label}</span>{count && <span className="ml-auto rounded-full bg-white/5 px-2 py-0.5 text-[10px]">{count}</span>}</button>; }
function Metric({ title, value, change, icon: Icon, children }: { title: string; value: string; change: string; icon: typeof Activity; children: React.ReactNode }) { return <Card className="relative min-h-40 border-white/7 bg-card/70"><CardHeader className="flex-row items-center justify-between"><span className="text-sm text-slate-400">{title}</span><Icon className="size-4 text-slate-600" /></CardHeader><CardContent><div className="flex items-end justify-between"><p className="text-2xl font-semibold tracking-tight">{value}</p><span className="text-sm text-slate-300">{change}</span></div>{children}</CardContent></Card>; }
function PositionRow({ symbol, side, weight, signal, price, move, profit }: (typeof positions)[number] & { profit: number }) { return <tr className="border-b border-white/5"><td className="px-4 py-4 font-semibold text-slate-100">{symbol}</td><td className="px-4 py-4">{side === 'LONG' ? 'Long' : 'Short'}</td><td className={`px-4 py-4 font-mono ${profit < 0 ? 'text-[#ff8b9e]' : profit > 0 ? 'text-[#73f0b5]' : 'text-slate-300'}`}><div>{signedMoney(profit)}</div><div className="mt-1 text-xs">{percent(signal)}</div></td><td className="px-4 py-4 font-mono">{Math.abs(weight).toFixed(1)}%</td><td className="px-4 py-4 font-mono">{currency(price)}</td><td className="px-4 py-4 text-right font-mono">{percent(move / 100)}</td></tr>; }

function BacktestMetric({ label, strategy, benchmark, positive }: { label: string; strategy: string; benchmark: string; positive?: boolean }) { return <div className="rounded-lg border border-white/6 bg-white/[.018] p-3"><p className="text-xs uppercase tracking-[.12em] text-slate-600">{label}</p><div className="mt-3 flex items-end justify-between"><div><p className={`font-mono text-lg font-semibold ${positive ? 'text-[#73f0b5]' : 'text-slate-100'}`}>{strategy}</p><p className="text-[10px] text-slate-600">NORTHSTAR</p></div><div className="text-right"><p className="font-mono text-sm text-slate-400">{benchmark}</p><p className="text-[10px] text-slate-600">SPY</p></div></div></div>; }
function RelativeMetric({ label, value, note, warning }: { label: string; value: string; note: string; warning?: boolean }) { return <div className="rounded-lg border border-white/6 bg-white/[.018] p-3"><p className="text-xs uppercase tracking-[.12em] text-slate-600">{label}</p><p className={`mt-2 font-mono text-lg font-semibold ${warning ? 'text-[#ff8b9e]' : 'text-slate-100'}`}>{value}</p><p className="mt-1 text-[10px] text-slate-600">{note}</p></div>; }
function Modal({ title, description, onClose, wide, children }: { title: string; description: string; onClose: () => void; wide?: boolean; children: React.ReactNode }) { return <div className="fixed inset-0 z-50 grid place-items-center bg-black/65 p-4 backdrop-blur-sm" role="dialog" aria-modal="true" aria-label={title} onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}><div className={`relative max-h-[88vh] w-full overflow-y-auto rounded-xl border border-white/8 bg-[#101720] p-5 shadow-2xl ${wide ? 'max-w-2xl' : 'max-w-lg'}`}><button className="absolute right-3 top-3 grid size-8 place-items-center rounded-lg text-slate-500 hover:bg-white/5 hover:text-white" onClick={onClose} aria-label="Close"><X className="size-4" /></button><div className="mb-4 pr-8"><h2 className="font-semibold">{title}</h2><p className="mt-1 text-sm text-slate-500">{description}</p></div><div className="space-y-4">{children}</div></div></div>; }
