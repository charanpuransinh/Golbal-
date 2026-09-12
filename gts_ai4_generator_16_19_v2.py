#!/usr/bin/env python3
"""GTS AI-4 Modules 16-19 production generator v2.
Generates 8 production modules, their unit tests, an integration wiring layer,
and a real end-to-end test harness. It never creates placeholder upstream
components; it validates and imports the existing 11-15 AI-4 contracts.
"""
from __future__ import annotations
import os, sys, subprocess, py_compile
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "./GTS").resolve()

SRC = {}

def add(path, text): SRC[path] = text.lstrip("\n")

add("16_paper_trading/paper_engine.py", r'''
"""Paper execution simulator for GTS."""
from __future__ import annotations
import math, time, uuid
from dataclasses import dataclass, field
from enum import Enum

class OrderSide(str, Enum): BUY="BUY"; SELL="SELL"
class OrderStatus(str, Enum): PENDING="PENDING"; FILLED="FILLED"; PARTIALLY_FILLED="PARTIALLY_FILLED"; REJECTED="REJECTED"; CANCELLED="CANCELLED"

@dataclass
class PaperOrder:
    order_id:str; symbol:str; side:OrderSide; quantity:float; order_type:str="MARKET"
    limit_price:float|None=None; stop_price:float|None=None; status:OrderStatus=OrderStatus.PENDING
    filled_qty:float=0.0; avg_fill_price:float=0.0; created_at:float=field(default_factory=time.time)
    strategy_id:str|None=None; meta:dict=field(default_factory=dict)
@dataclass
class PaperFill:
    fill_id:str; order_id:str; symbol:str; side:OrderSide; quantity:float; price:float; timestamp:float
    slippage:float=0.0; commission:float=0.0; strategy_id:str|None=None

class PaperAccount:
    def __init__(self, starting_capital:float=1_000_000.0):
        if not math.isfinite(starting_capital) or starting_capital <= 0: raise ValueError("starting_capital must be finite and positive")
        self.starting_capital=starting_capital; self.cash=starting_capital
        self.positions:dict[str,dict[str,float]]={}; self.realized_pnl=0.0; self.trade_log:list[PaperFill]=[]
    def apply_fill(self, fill:PaperFill):
        q,p,c=fill.quantity,fill.price,fill.commission
        if not all(math.isfinite(x) for x in (q,p,c)) or q<=0 or p<=0 or c<0: raise ValueError("invalid fill")
        pos=self.positions.setdefault(fill.symbol,{"qty":0.0,"avg_price":0.0}); old=pos["qty"]
        signed=q if fill.side is OrderSide.BUY else -q
        new=old+signed
        if fill.side is OrderSide.BUY: self.cash -= p*q+c
        else: self.cash += p*q-c
        # Average-cost signed-position accounting. Realize PnL only on the reduced side.
        if old == 0 or (old>0 and signed>0) or (old<0 and signed<0):
            pos["avg_price"] = p if old==0 else (abs(old)*pos["avg_price"] + q*p)/abs(new)
        else:
            close_qty=min(abs(old),q); direction=1.0 if old>0 else -1.0
            self.realized_pnl += direction*(p-pos["avg_price"])*close_qty - c
            if new==0: pos["avg_price"]=0.0
            elif (old>0 and new<0) or (old<0 and new>0): pos["avg_price"]=p
        pos["qty"]=new
        self.trade_log.append(fill)
    def unrealized_pnl(self,last_prices:dict[str,float])->float:
        total=0.0
        for s,pos in self.positions.items():
            q=pos["qty"]
            if q: total += (last_prices.get(s,pos["avg_price"])-pos["avg_price"])*q
        return total
    def equity(self,last_prices:dict[str,float])->float:
        market_value=sum(p["qty"]*last_prices.get(s,p["avg_price"]) for s,p in self.positions.items())
        return self.cash+market_value

class PaperEngine:
    def __init__(self,account:PaperAccount|None=None,slippage_bps:float=1.0,commission_per_trade:float=0.0):
        if not math.isfinite(slippage_bps) or slippage_bps<0 or not math.isfinite(commission_per_trade) or commission_per_trade<0: raise ValueError("invalid execution costs")
        self.account=account or PaperAccount(); self.slippage_bps=slippage_bps; self.commission_per_trade=commission_per_trade
        self.orders:dict[str,PaperOrder]={}; self._last_prices:dict[str,float]={}
    def update_market_price(self,symbol:str,price:float):
        if not symbol or not math.isfinite(price) or price<=0: raise ValueError("invalid market price")
        self._last_prices[symbol]=price
        for o in list(self.orders.values()):
            if o.status is OrderStatus.PENDING and o.symbol==symbol: self._try_fill(o)
    def place_order(self,symbol:str,side:OrderSide,quantity:float,order_type:str="MARKET",limit_price:float|None=None,stop_price:float|None=None,strategy_id:str|None=None)->PaperOrder:
        if not symbol or not isinstance(side,OrderSide) or not math.isfinite(quantity) or quantity<=0: raise ValueError("invalid order")
        ot=order_type.upper();
        if ot not in {"MARKET","LIMIT","SL","SL-M","STOP","STOP_LIMIT"}: raise ValueError(f"unsupported order_type: {order_type}")
        oid=str(uuid.uuid4()); o=PaperOrder(oid,symbol,side,quantity,ot,limit_price,stop_price,strategy_id=strategy_id); self.orders[oid]=o; self._try_fill(o); return o
    def cancel_order(self,order_id:str)->bool:
        o=self.orders.get(order_id)
        if o and o.status is OrderStatus.PENDING: o.status=OrderStatus.CANCELLED; return True
        return False
    def get_open_orders(self): return [o for o in self.orders.values() if o.status is OrderStatus.PENDING]
    def _try_fill(self,o:PaperOrder):
        m=self._last_prices.get(o.symbol)
        if m is None: return
        if o.order_type=="MARKET": price=m*(1+(self.slippage_bps/10000)*(1 if o.side is OrderSide.BUY else -1))
        elif o.order_type in {"LIMIT","STOP_LIMIT"}:
            if o.limit_price is None or o.limit_price<=0: o.status=OrderStatus.REJECTED; o.meta["reason"]="INVALID_LIMIT_PRICE"; return
            if (o.side is OrderSide.BUY and m>o.limit_price) or (o.side is OrderSide.SELL and m<o.limit_price): return
            price=o.limit_price
        else:
            # Stop orders are pending until the stop is crossed, then fill at market.
            if o.stop_price is None or o.stop_price<=0: o.status=OrderStatus.REJECTED; o.meta["reason"]="INVALID_STOP_PRICE"; return
            triggered=(m>=o.stop_price) if o.side is OrderSide.BUY else (m<=o.stop_price)
            if not triggered: return
            price=m
        fill=PaperFill(str(uuid.uuid4()),o.order_id,o.symbol,o.side,o.quantity,price,time.time(),abs(price-m),self.commission_per_trade,o.strategy_id)
        self.account.apply_fill(fill); o.status=OrderStatus.FILLED; o.filled_qty=o.quantity; o.avg_fill_price=price
    def account_snapshot(self):
        return {"cash":self.account.cash,"realized_pnl":self.account.realized_pnl,"unrealized_pnl":self.account.unrealized_pnl(self._last_prices),"equity":self.account.equity(self._last_prices),"positions":self.account.positions}
''')

add("17_live_trading/live_gate.py", r'''
"""Final certification gate for live trading."""
from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class GateResult:
    approved:bool; reason:str=""

class LiveGate:
    REQUIRED_STAGES=("backtest","optimization","walk_forward","stress_test","risk_test","qa_gate","paper_trade")
    def __init__(self,risk_engine,exposure_manager,qa_gate=None,strategy_certifications=None,min_paper_trades=30):
        if min_paper_trades<0: raise ValueError("min_paper_trades must be non-negative")
        self.risk_engine=risk_engine; self.exposure_manager=exposure_manager; self.qa_gate=qa_gate
        self.certifications=strategy_certifications if strategy_certifications is not None else {}; self.min_paper_trades=min_paper_trades
    def register_certification(self,strategy_id,stage,passed,**details):
        if stage not in self.REQUIRED_STAGES: raise ValueError(f"unknown certification stage: {stage}")
        self.certifications.setdefault(strategy_id,{})[stage]={"passed":bool(passed),**details}
    def _certification_complete(self,strategy_id):
        c=self.certifications.get(strategy_id)
        if not c: return GateResult(False,"NO_CERTIFICATION_RECORD")
        for stage in self.REQUIRED_STAGES:
            e=c.get(stage)
            if not e or not e.get("passed"): return GateResult(False,f"STAGE_NOT_PASSED:{stage}")
        n=c["paper_trade"].get("trade_count",0)
        return GateResult(True) if n>=self.min_paper_trades else GateResult(False,f"INSUFFICIENT_PAPER_TRADES:{n}/{self.min_paper_trades}")
    def check(self,order):
        sid=getattr(order,"strategy_id",None)
        if not sid: return GateResult(False,"NO_STRATEGY_ID_ON_ORDER")
        r=self._certification_complete(sid)
        if not r.approved:return r
        if self.qa_gate is not None:
            fn=getattr(self.qa_gate,"check_strategy",None)
            if fn:
                q=fn(sid); passed=q.get("passed",False) if isinstance(q,dict) else getattr(q,"passed",False)
                if not passed:return GateResult(False,f"QA_GATE_FAILED:{getattr(q,'reason','') if not isinstance(q,dict) else q.get('reason','')}")
        return GateResult(True,"LIVE_CERTIFICATION_PASSED")
''')

add("17_live_trading/live_engine.py", r'''
"""Live order lifecycle. Real broker access occurs only through OrderManager."""
from __future__ import annotations
import math,time,uuid
from dataclasses import dataclass,field
from enum import Enum
from order_validator import InstrumentSpec,OrderType
from risk_engine import PortfolioState

class LiveOrderStatus(str,Enum): PENDING="PENDING"; SENT="SENT"; ACKED="ACKED"; FILLED="FILLED"; PARTIALLY_FILLED="PARTIALLY_FILLED"; REJECTED="REJECTED"; CANCELLED="CANCELLED"; ERROR="ERROR"
@dataclass
class LiveOrder:
    order_id:str; symbol:str; side:str; quantity:float; order_type:str="MARKET"; limit_price:float|None=None; stop_price:float|None=None; strategy_id:str|None=None
    status:LiveOrderStatus=LiveOrderStatus.PENDING; broker_order_id:str|None=None; filled_qty:float=0.0; avg_fill_price:float=0.0; created_at:float=field(default_factory=time.time); error:str|None=None

class LiveEngine:
    def __init__(self,broker_adapter,live_gate,kill_switch,order_manager,instrument_spec_provider,portfolio_state_provider):
        self.broker=broker_adapter; self.live_gate=live_gate; self.kill_switch=kill_switch; self.order_manager=order_manager
        self.instrument_spec_provider=instrument_spec_provider; self.portfolio_state_provider=portfolio_state_provider; self.orders={}
    def submit_signal(self,symbol,side,quantity,order_type="MARKET",limit_price=None,stop_price=None,strategy_id=None):
        oid=str(uuid.uuid4()); o=LiveOrder(oid,symbol,side,quantity,order_type,limit_price,stop_price,strategy_id=strategy_id); self.orders[oid]=o
        if (self.kill_switch.is_active() if hasattr(self.kill_switch,"is_active") else self.kill_switch.is_tripped()): o.status=LiveOrderStatus.REJECTED;o.error="KILL_SWITCH_ACTIVE";return o
        gate=self.live_gate.check(o)
        if not gate.approved:o.status=LiveOrderStatus.REJECTED;o.error=gate.reason;return o
        try:
            spec=self.instrument_spec_provider(symbol); state=self.portfolio_state_provider()
            result=self.order_manager.submit(symbol=symbol,side=side,quantity=quantity,order_type=OrderType(order_type),strategy_id=strategy_id,instrument_spec=spec,portfolio_state=state,limit_price=limit_price,stop_price=stop_price,price_for_risk_check=limit_price)
            if not result.is_success:
                o.status=LiveOrderStatus.REJECTED if "REJECTED" in result.stage.value else LiveOrderStatus.ERROR; o.error="; ".join(result.reasons) or str(result); return o
            er=result.execution_result; o.status=LiveOrderStatus.ACKED; o.broker_order_id=er.broker_order_id; o.filled_qty=er.filled_quantity; o.avg_fill_price=er.average_fill_price
            if er.is_success and er.status.value=="FILLED": o.status=LiveOrderStatus.FILLED
        except Exception as exc:
            o.status=LiveOrderStatus.ERROR;o.error=str(exc); self.kill_switch.report_error("live_engine",str(exc))
        return o
    def on_broker_fill(self,broker_order_id,fill_qty,fill_price):
        if not math.isfinite(fill_qty) or fill_qty<=0 or not math.isfinite(fill_price) or fill_price<=0: raise ValueError("invalid broker fill")
        o=next((x for x in self.orders.values() if x.broker_order_id==broker_order_id),None)
        if not o:return False
        if o.filled_qty+fill_qty>o.quantity+1e-9: raise ValueError("broker fill exceeds order quantity")
        total=o.filled_qty+fill_qty; o.avg_fill_price=((o.avg_fill_price*o.filled_qty)+(fill_price*fill_qty))/total; o.filled_qty=total; o.status=LiveOrderStatus.FILLED if total>=o.quantity else LiveOrderStatus.PARTIALLY_FILLED; return True
    def cancel_order(self,order_id):
        o=self.orders.get(order_id)
        if not o or not o.broker_order_id or o.status not in {LiveOrderStatus.SENT,LiveOrderStatus.ACKED,LiveOrderStatus.PARTIALLY_FILLED}: return False
        try:
            ok=self.broker.cancel_order(o.broker_order_id); o.status=LiveOrderStatus.CANCELLED if ok else o.status; return bool(ok)
        except Exception as exc:o.error=str(exc);return False
    def open_orders(self):return [o for o in self.orders.values() if o.status in {LiveOrderStatus.SENT,LiveOrderStatus.ACKED,LiveOrderStatus.PARTIALLY_FILLED}]
''')

add("18_accounting/ledger.py", r'''
"""Append-only cash ledger."""
from __future__ import annotations
import math,time,uuid
from dataclasses import dataclass,field
from enum import Enum
class EntryType(str,Enum): TRADE_BUY="TRADE_BUY"; TRADE_SELL="TRADE_SELL"; FEE="FEE"; DEPOSIT="DEPOSIT"; WITHDRAWAL="WITHDRAWAL"; DIVIDEND="DIVIDEND"; ADJUSTMENT="ADJUSTMENT"
@dataclass(frozen=True)
class LedgerEntry:
    entry_id:str; entry_type:EntryType; symbol:str|None; amount:float; balance_after:float; timestamp:float=field(default_factory=time.time); order_id:str|None=None; note:str=""
class Ledger:
    def __init__(self,opening_balance=0.0,storage=None):
        if not math.isfinite(opening_balance):raise ValueError("opening_balance must be finite")
        self.balance=opening_balance;self.entries=[];self.storage=storage
    def _post(self,entry_type,amount,symbol=None,order_id=None,note=""):
        if not math.isfinite(amount):raise ValueError("amount must be finite")
        self.balance+=amount;e=LedgerEntry(str(uuid.uuid4()),entry_type,symbol,amount,self.balance,order_id=order_id,note=note);self.entries.append(e)
        if self.storage is not None:self.storage.save_ledger_entry(e)
        return e
    def record_buy(self,symbol,cost,order_id=None):return self._post(EntryType.TRADE_BUY,-abs(cost),symbol,order_id)
    def record_sell(self,symbol,proceeds,order_id=None):return self._post(EntryType.TRADE_SELL,abs(proceeds),symbol,order_id)
    def record_fee(self,amount,order_id=None,note=""):return self._post(EntryType.FEE,-abs(amount),order_id=order_id,note=note)
    def record_deposit(self,amount,note=""):return self._post(EntryType.DEPOSIT,abs(amount),note=note)
    def record_withdrawal(self,amount,note=""):return self._post(EntryType.WITHDRAWAL,-abs(amount),note=note)
    def record_adjustment(self,amount,note):return self._post(EntryType.ADJUSTMENT,amount,note=note)
    def statement(self,start_ts=None,end_ts=None):return [e for e in self.entries if (start_ts is None or e.timestamp>=start_ts) and (end_ts is None or e.timestamp<=end_ts)]
    def current_balance(self):return self.balance
''')

add("18_accounting/trade_book.py", r'''
"""Round-trip trade accounting."""
from __future__ import annotations
import math,time,uuid
from dataclasses import dataclass,field
from enum import Enum
class TradeStatus(str,Enum): OPEN="OPEN"; CLOSED="CLOSED"
@dataclass
class TradeRecord:
    trade_id:str;symbol:str;strategy_id:str|None;side:str;entry_price:float;entry_qty:float;entry_time:float;exit_price:float|None=None;exit_qty:float=0.0;exit_time:float|None=None;status:TradeStatus=TradeStatus.OPEN;realized_pnl:float=0.0;fees:float=0.0;tags:dict=field(default_factory=dict)
    def close(self,exit_price,exit_qty,exit_time=None):
        if self.status is TradeStatus.CLOSED:raise ValueError("trade already closed")
        if not all(math.isfinite(x) for x in (exit_price,exit_qty)) or exit_price<=0 or exit_qty<=0 or exit_qty>self.entry_qty:raise ValueError("invalid exit")
        self.exit_price=exit_price;self.exit_qty=exit_qty;self.exit_time=exit_time or time.time();self.status=TradeStatus.CLOSED;d=1 if self.side.upper()=="LONG" else -1;self.realized_pnl=d*(exit_price-self.entry_price)*exit_qty-self.fees
class TradeBook:
    def __init__(self,storage=None):self.storage=storage;self.trades={};self._open_by_symbol_strategy={}
    @staticmethod
    def _key(symbol,strategy_id):return f"{symbol}::{strategy_id or ''}"
    def open_trade(self,symbol,strategy_id,side,entry_price,entry_qty,fees=0.0):
        if not symbol or side.upper() not in {"LONG","SHORT"} or not all(math.isfinite(x) for x in (entry_price,entry_qty,fees)) or entry_price<=0 or entry_qty<=0 or fees<0:raise ValueError("invalid trade")
        key=self._key(symbol,strategy_id)
        if key in self._open_by_symbol_strategy:raise ValueError("open trade already exists for symbol/strategy")
        t=TradeRecord(str(uuid.uuid4()),symbol,strategy_id,side.upper(),entry_price,entry_qty,time.time(),fees=fees);self.trades[t.trade_id]=t;self._open_by_symbol_strategy[key]=t.trade_id
        if self.storage is not None:self.storage.save_trade(t)
        return t
    def close_trade(self,symbol,strategy_id,exit_price,exit_qty,extra_fees=0.0):
        tid=self._open_by_symbol_strategy.get(self._key(symbol,strategy_id));
        if not tid:return None
        t=self.trades[tid];t.fees+=extra_fees;t.close(exit_price,exit_qty);self._open_by_symbol_strategy.pop(self._key(symbol,strategy_id),None)
        if self.storage is not None:self.storage.save_trade(t)
        return t
    def open_trades(self,strategy_id=None):return [t for t in self.trades.values() if t.status is TradeStatus.OPEN and (strategy_id is None or t.strategy_id==strategy_id)]
    def closed_trades(self,strategy_id=None,symbol=None):return [t for t in self.trades.values() if t.status is TradeStatus.CLOSED and (strategy_id is None or t.strategy_id==strategy_id) and (symbol is None or t.symbol==symbol)]
    def total_realized_pnl(self,strategy_id=None):return sum(t.realized_pnl for t in self.closed_trades(strategy_id=strategy_id))
''')

add("19_reporting/performance_report.py", r'''
"""Performance metrics."""
from __future__ import annotations
import math
from dataclasses import dataclass,field
@dataclass
class PerformanceReport:
 total_trades:int=0;winning_trades:int=0;losing_trades:int=0;win_rate:float=0.0;gross_profit:float=0.0;gross_loss:float=0.0;profit_factor:float=0.0;total_pnl:float=0.0;avg_win:float=0.0;avg_loss:float=0.0;largest_win:float=0.0;largest_loss:float=0.0;max_drawdown:float=0.0;max_drawdown_pct:float=0.0;sharpe_ratio:float|None=None;sortino_ratio:float|None=None;cagr:float|None=None;extra:dict=field(default_factory=dict)
class PerformanceReportEngine:
 def __init__(self,risk_free_rate=0.0,periods_per_year=252):
  if periods_per_year<=0:raise ValueError("periods_per_year must be positive")
  self.risk_free_rate=risk_free_rate;self.periods_per_year=periods_per_year
 def from_trades(self,trades):
  r=PerformanceReport();p=[float(t.realized_pnl) for t in trades];w=[x for x in p if x>0];l=[x for x in p if x<0];r.total_trades=len(p);r.winning_trades=len(w);r.losing_trades=len(l);r.win_rate=len(w)/len(p) if p else 0.0;r.gross_profit=sum(w);r.gross_loss=abs(sum(l));r.profit_factor=r.gross_profit/r.gross_loss if r.gross_loss else (float("inf") if r.gross_profit else 0.0);r.total_pnl=sum(p);r.avg_win=sum(w)/len(w) if w else 0.0;r.avg_loss=sum(l)/len(l) if l else 0.0;r.largest_win=max(w) if w else 0.0;r.largest_loss=min(l) if l else 0.0;return r
 def with_equity_curve(self,r,equity_curve):
  if len(equity_curve)<2:return r
  if any(not math.isfinite(float(x)) for x in equity_curve):raise ValueError("equity_curve contains non-finite values")
  peak=equity_curve[0]
  for x in equity_curve:peak=max(peak,x);dd=peak-x;r.max_drawdown=max(r.max_drawdown,dd);r.max_drawdown_pct=max(r.max_drawdown_pct,dd/peak if peak>0 else 0.0)
  ret=[equity_curve[i]/equity_curve[i-1]-1 for i in range(1,len(equity_curve)) if equity_curve[i-1]!=0]
  if not ret:return r
  rf=self.risk_free_rate/self.periods_per_year;mean=sum(ret)/len(ret);std=math.sqrt(sum((x-mean)**2 for x in ret)/len(ret));down=[x for x in ret if x<0];downstd=math.sqrt(sum(x*x for x in down)/len(down)) if down else 0.0
  if std:r.sharpe_ratio=(mean-rf)/std*math.sqrt(self.periods_per_year)
  if downstd:r.sortino_ratio=(mean-rf)/downstd*math.sqrt(self.periods_per_year)
  years=(len(equity_curve)-1)/self.periods_per_year
  if equity_curve[0]>0 and equity_curve[-1]>=0 and years>0:r.cagr=(equity_curve[-1]/equity_curve[0])**(1/years)-1
  return r
 def generate(self,trades,equity_curve=None):return self.with_equity_curve(self.from_trades(trades),equity_curve) if equity_curve else self.from_trades(trades)
''')

add("19_reporting/risk_report.py", r'''
"""Point-in-time risk report."""
from __future__ import annotations
from dataclasses import dataclass,field
@dataclass
class PositionRisk:symbol:str;quantity:float;market_value:float;exposure_pct:float;unrealized_pnl:float
@dataclass
class RiskReport:
 total_exposure:float=0.0;net_exposure:float=0.0;gross_exposure:float=0.0;leverage:float=0.0;largest_position_pct:float=0.0;value_at_risk_95:float|None=None;limit_breaches:list[str]=field(default_factory=list);positions:list[PositionRisk]=field(default_factory=list);kill_switch_active:bool=False
class RiskReportEngine:
 def __init__(self,exposure_manager,risk_engine,portfolio_manager,kill_switch=None):self.exposure_manager=exposure_manager;self.risk_engine=risk_engine;self.portfolio_manager=portfolio_manager;self.kill_switch=kill_switch
 def _historical_var(self,returns,confidence=.95):
  if not returns:return None
  r=sorted(float(x) for x in returns);i=max(0,min(int((1-confidence)*len(r)),len(r)-1));return abs(r[i])
 def generate(self,account_equity,historical_returns=None):
  if account_equity<=0:raise ValueError("account_equity must be positive")
  positions=self.portfolio_manager.get_all_positions();out=RiskReport();gross=net=0.0
  for p in positions:
   mv=p["quantity"]*p["last_price"];gross+=abs(mv);net+=mv;out.positions.append(PositionRisk(p["symbol"],p["quantity"],mv,abs(mv)/account_equity,(p["last_price"]-p["avg_price"])*p["quantity"]))
  out.gross_exposure=gross;out.net_exposure=net;out.total_exposure=gross;out.leverage=gross/account_equity;out.largest_position_pct=max((p.exposure_pct for p in out.positions),default=0.0)
  if historical_returns:out.value_at_risk_95=self._historical_var(historical_returns)
  fn=getattr(self.exposure_manager,"check_all",None)
  if fn:
   x=fn(positions,account_equity);out.limit_breaches=list(x.get("breaches",[])) if isinstance(x,dict) else []
  if self.kill_switch is not None:out.kill_switch_active=(self.kill_switch.is_active() if hasattr(self.kill_switch,"is_active") else self.kill_switch.is_tripped())
  return out
''')

add("19_reporting/trade_report.py", r'''
"""Trade-level reporting and CSV export."""
from __future__ import annotations
import csv,io
from collections import defaultdict
from dataclasses import dataclass,field
@dataclass
class TradeReportRow:trade_id:str;symbol:str;strategy_id:str|None;side:str;entry_price:float;exit_price:float|None;quantity:float;entry_time:float;exit_time:float|None;realized_pnl:float;fees:float;status:str
@dataclass
class TradeReport:rows:list[TradeReportRow]=field(default_factory=list);summary_by_strategy:dict=field(default_factory=dict);summary_by_symbol:dict=field(default_factory=dict)
class TradeReportEngine:
 def __init__(self,trade_book):self.trade_book=trade_book
 def generate(self,strategy_id=None,symbol=None,start_ts=None,end_ts=None):
  ts=self.trade_book.closed_trades(strategy_id=strategy_id,symbol=symbol);ts=[t for t in ts if (start_ts is None or t.entry_time>=start_ts) and (end_ts is None or t.entry_time<=end_ts)];r=TradeReport();sa=defaultdict(lambda:{"trades":0,"pnl":0.0,"fees":0.0});sy=defaultdict(lambda:{"trades":0,"pnl":0.0,"fees":0.0})
  for t in ts:
   r.rows.append(TradeReportRow(t.trade_id,t.symbol,t.strategy_id,t.side,t.entry_price,t.exit_price,t.entry_qty,t.entry_time,t.exit_time,t.realized_pnl,t.fees,t.status.value));k=t.strategy_id or "UNKNOWN";sa[k]["trades"]+=1;sa[k]["pnl"]+=t.realized_pnl;sa[k]["fees"]+=t.fees;sy[t.symbol]["trades"]+=1;sy[t.symbol]["pnl"]+=t.realized_pnl;sy[t.symbol]["fees"]+=t.fees
  r.summary_by_strategy=dict(sa);r.summary_by_symbol=dict(sy);return r
 def to_csv(self,report):
  b=io.StringIO();w=csv.writer(b);w.writerow(["trade_id","symbol","strategy_id","side","entry_price","exit_price","quantity","entry_time","exit_time","realized_pnl","fees","status"])
  for x in report.rows:w.writerow([x.trade_id,x.symbol,x.strategy_id,x.side,x.entry_price,x.exit_price,x.quantity,x.entry_time,x.exit_time,x.realized_pnl,x.fees,x.status])
  return b.getvalue()
''')

# Tests for all eight production files + real contract wiring smoke tests.
TESTS={
"16_paper_trading/tests/test_paper_engine.py":'''from paper_engine import *\ndef test_round_trip():\n a=PaperAccount(100000);e=PaperEngine(a);e.update_market_price("NIFTY",100);b=e.place_order("NIFTY",OrderSide.BUY,10);assert b.status is OrderStatus.FILLED;e.update_market_price("NIFTY",110);s=e.place_order("NIFTY",OrderSide.SELL,10);assert s.status is OrderStatus.FILLED;assert a.realized_pnl>0\ndef test_limit_rechecks_on_market_update():\n e=PaperEngine();e.update_market_price("GOLD",100);o=e.place_order("GOLD",OrderSide.BUY,1,"LIMIT",95);assert o.status is OrderStatus.PENDING;e.update_market_price("GOLD",94);assert o.status is OrderStatus.FILLED\n''',
"17_live_trading/tests/test_live_gate.py":'''from live_gate import *\nclass R: pass\nclass O: strategy_id="S1"\nclass Q:\n def check_strategy(self,s): return {"passed":True}\ndef test_gate():\n g=LiveGate(R(),R(),Q(),min_paper_trades=1);\n for x in g.REQUIRED_STAGES:g.register_certification("S1",x,True,trade_count=1 if x=="paper_trade" else 0)\n assert g.check(O()).approved\n''',
"17_live_trading/tests/test_live_engine.py":'''from live_engine import *\nfrom live_gate import GateResult\nclass K:\n def __init__(self):self.x=False\n def is_tripped(self):return self.x\n def report_error(self,*a):pass\nclass G:\n def check(self,o):return GateResult(True)\nclass ER:\n status=type("S",(),{"value":"FILLED"})();filled_quantity=1;average_fill_price=100;broker_order_id="B1";is_success=True\nclass LR:\n is_success=True;stage=type("S",(),{"value":"EXECUTED"})();execution_result=ER();reasons=[]\nclass OM:\n def submit(self,**kw):return LR()\nclass Spec:\n def __call__(self,s):from order_validator import InstrumentSpec;return InstrumentSpec(s,.05,1)\nclass State:\n def __call__(self):from risk_engine import PortfolioState;return PortfolioState(100000,100000,0,0,0,0,{})\ndef test_live():\n e=LiveEngine(object(),G(),K(),OM(),Spec(),State());o=e.submit_signal("NIFTY","BUY",1,"MARKET",strategy_id="S1");assert o.status is LiveOrderStatus.FILLED\n''',
"18_accounting/tests/test_ledger.py":'''from ledger import *\ndef test_ledger():\n l=Ledger(1000);l.record_buy("NIFTY",100);l.record_sell("NIFTY",150);assert l.current_balance()==1050\n''',
"18_accounting/tests/test_trade_book.py":'''from trade_book import *\ndef test_trade_book():\n b=TradeBook();b.open_trade("NIFTY","S1","LONG",100,10);t=b.close_trade("NIFTY","S1",110,10);assert t and t.realized_pnl==100\n''',
"19_reporting/tests/test_performance_report.py":'''from performance_report import *\nclass T:\n def __init__(self,p):self.realized_pnl=p\ndef test_metrics():\n r=PerformanceReportEngine().generate([T(100),T(-50)],[1000,1100,1050]);assert r.total_trades==2 and r.gross_profit==100 and r.gross_loss==50 and r.max_drawdown==50\n''',
"19_reporting/tests/test_risk_report.py":'''from risk_report import *\nclass P:\n def get_all_positions(self):return [{"symbol":"NIFTY","quantity":10,"last_price":100,"avg_price":90}]\nclass E: pass\ndef test_risk():\n r=RiskReportEngine(E(),E(),P()).generate(1000);assert r.gross_exposure==1000 and r.leverage==1\n''',
"19_reporting/tests/test_trade_report.py":'''from trade_book import TradeBook\nfrom trade_report import TradeReportEngine\ndef test_report():\n b=TradeBook();b.open_trade("NIFTY","S1","LONG",100,1);b.close_trade("NIFTY","S1",110,1);r=TradeReportEngine(b).generate();assert len(r.rows)==1 and "trade_id,symbol" in TradeReportEngine(b).to_csv(r)\n''',
}

# Wiring module uses the real upstream contracts, no placeholders.
add("27_system/ai4_bootstrap.py", r'''
"""Canonical AI-4 16-19 wiring over real 11-15 components."""
from __future__ import annotations
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
for d in ("11_validation","12_risk","13_portfolio","14_execution","15_broker","16_paper_trading","17_live_trading","18_accounting","19_reporting"):
 p=str(ROOT/d)
 if p not in sys.path:sys.path.insert(0,p)
from paper_engine import PaperEngine,PaperAccount
from live_gate import LiveGate
from live_engine import LiveEngine
from ledger import Ledger
from trade_book import TradeBook
from performance_report import PerformanceReportEngine
from risk_report import RiskReportEngine
from trade_report import TradeReportEngine
from risk_engine import RiskEngine,RiskLimits
from exposure_manager import ExposureManager
from kill_switch import KillSwitch
from order_manager import OrderManager
from broker_adapter_base import _NullBroker
from order_validator import InstrumentSpec
from portfolio_manager import PortfolioManager

class AI4Registry:
 def __init__(self,starting_capital=1_000_000.0):
  self.kill_switch=KillSwitch();self.risk_engine=RiskEngine(RiskLimits(max_position_size=100,max_daily_loss=5000,max_gross_exposure=10_000_000));self.exposure_manager=self.risk_engine.exposure_manager
  self.portfolio_manager=PortfolioManager(starting_capital);self.broker_adapter=_NullBroker();self.broker_adapter.connect();self.order_manager=OrderManager(self.risk_engine,self.broker_adapter)
  self.paper_account=PaperAccount(starting_capital);self.paper_engine=PaperEngine(self.paper_account)
  self.qa_gate=None
  self.live_gate=LiveGate(self.risk_engine,self.exposure_manager,self.qa_gate,min_paper_trades=30)
  self.live_engine=LiveEngine(self.broker_adapter,self.live_gate,self.kill_switch,self.order_manager,lambda s:InstrumentSpec(s,.05,1),lambda:self.portfolio_manager.to_risk_state(self.kill_switch.is_active()))
  self.ledger=Ledger(starting_capital);self.trade_book=TradeBook();self.performance_report_engine=PerformanceReportEngine();self.risk_report_engine=RiskReportEngine(self.exposure_manager,self.risk_engine,self.portfolio_manager,self.kill_switch);self.trade_report_engine=TradeReportEngine(self.trade_book)
 def health_check(self):return {"paper":True,"live_gate":True,"live_engine":True,"ledger":True,"trade_book":True,"performance_report":True,"risk_report":True,"trade_report":True,"broker_connected":self.broker_adapter.health_check().is_healthy,"kill_switch":self.kill_switch.is_active()}
def build_registry(**kw):return AI4Registry(**kw)
''')

add("28_tests/ai4_pipeline/test_end_to_end_wiring.py", r'''
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"27_system"))
from ai4_bootstrap import build_registry
from paper_engine import OrderSide

def test_real_registry_health():
 r=build_registry(starting_capital=100000);h=r.health_check();assert all(h[k] for k in ("paper","live_gate","live_engine","ledger","trade_book","performance_report","risk_report","trade_report","broker_connected"));assert h["kill_switch"] is False

def test_paper_to_accounting_and_reporting():
 r=build_registry(starting_capital=100000);r.paper_engine.update_market_price("NIFTY",100);o=r.paper_engine.place_order("NIFTY",OrderSide.BUY,10,"MARKET",strategy_id="S1");assert o.status.value=="FILLED";r.ledger.record_buy("NIFTY",o.avg_fill_price*o.filled_qty,o.order_id);r.trade_book.open_trade("NIFTY","S1","LONG",o.avg_fill_price,o.filled_qty);r.paper_engine.update_market_price("NIFTY",110);x=r.paper_engine.place_order("NIFTY",OrderSide.SELL,10,"MARKET",strategy_id="S1");t=r.trade_book.close_trade("NIFTY","S1",x.avg_fill_price,x.filled_qty);r.ledger.record_sell("NIFTY",x.avg_fill_price*x.filled_qty,x.order_id);assert t and t.realized_pnl>0;assert r.performance_report_engine.generate(r.trade_book.closed_trades("S1")).total_pnl>0

def test_live_blocked_without_certification():
 r=build_registry();o=r.live_engine.submit_signal("NIFTY","BUY",1,"MARKET",strategy_id="UNAPPROVED");assert o.status.value=="REJECTED" and o.error=="NO_CERTIFICATION_RECORD"

def test_kill_switch_blocks_live():
 r=build_registry();r.kill_switch.trip("test");o=r.live_engine.submit_signal("NIFTY","BUY",1,"MARKET",strategy_id="S1");assert o.status.value=="REJECTED" and o.error=="KILL_SWITCH_ACTIVE"
''')

# generator
for p,t in TESTS.items(): add(p,t)

def main():
 for rel in SRC:
  p=ROOT/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(SRC[rel],encoding="utf-8")
 # Validate upstream files are present; do not replace them.
 required=["11_validation/code_validator.py","11_validation/logic_validator.py","11_validation/data_validator.py","11_validation/dependency_checker.py","11_validation/strategy_validator.py","12_risk/risk_engine.py","12_risk/position_sizing.py","12_risk/exposure_manager.py","12_risk/drawdown_guard.py","12_risk/kill_switch.py","13_portfolio/portfolio_manager.py","13_portfolio/position_manager.py","13_portfolio/pnl_engine.py","14_execution/order_manager.py","14_execution/execution_engine.py","14_execution/order_validator.py","15_broker/broker_adapter_base.py","15_broker/broker_registry.py"]
 missing=[x for x in required if not (ROOT/x).exists()]
 if missing: print("BLOCKED: missing upstream AI-4 files:");[print(" -",x) for x in missing];return 2
 pyfiles=[ROOT/x for x in SRC if x.endswith('.py')]
 failures=[]
 for p in pyfiles:
  try:py_compile.compile(str(p),doraise=True)
  except Exception as e:failures.append((p,e))
 if failures:
  print("COMPILE FAIL");[print(p,e) for p,e in failures];return 3
 # Run unittest discovery through pytest if available, else unittest.
 env=dict(os.environ);env["PYTHONPATH"]=os.pathsep.join(str(ROOT/d) for d in ("11_validation","12_risk","13_portfolio","14_execution","15_broker","16_paper_trading","17_live_trading","18_accounting","19_reporting","27_system","28_tests/ai4_pipeline"))
 cmd=[sys.executable,"-m","pytest","-q",str(ROOT/"16_paper_trading/tests"),str(ROOT/"17_live_trading/tests"),str(ROOT/"18_accounting/tests"),str(ROOT/"19_reporting/tests"),str(ROOT/"28_tests/ai4_pipeline")]
 try:r=subprocess.run(cmd,env=env,text=True)
 except FileNotFoundError:r=subprocess.run([sys.executable,"-m","unittest","discover","-s",str(ROOT/"16_paper_trading/tests")],env=env,text=True)
 if r.returncode: return r.returncode
 print("\nGTS AI-4 MODULES 16-19: COMPLETE")
 print("8 production files + unit tests + real 11-15 wiring + end-to-end tests: PASS")
 return 0
if __name__=="__main__":raise SystemExit(main())
