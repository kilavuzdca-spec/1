import aiohttp
import asyncio
import os
import logging
from datetime import datetime

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# GitHub Secrets'tan token'ı al - GÜVENLİ
BOT_TOKEN = os.getenv('BOT_TOKEN')  # Sadece bu satır - token koddan kaldırıldı!

if not BOT_TOKEN:
    logger.error("❌ BOT_TOKEN environment variable ayarlanmamış!")
    logger.info("📝 GitHub Repository Settings → Secrets and variables → Actions'a BOT_TOKEN ekleyin")

# Diğer sabitler
TIMEFRAMES = ["1h", "4h", "1d", "1w", "1M"] 
SM = 21
CD = 0.4
KLINE_LIMIT = 100  # GitHub için düşük limit

# --- ULTRAFILTER İÇİN GEREKLİ TANIMLAMALAR ---
SUB_TFS = {
    "1h": ["30m","15m","5m"],
    "4h": ["2h","1h","30m","15m"],
    "1d": ["12h","8h","6h","4h","2h","1h"],
    "1w": ["3d","1d","12h","8h","6h","4h"],
    "1M": ["1w","3d","1d","12h","8h"]
}

SIGNAL_POWER = {3:1.0, 6:1.5, 7:1.8, 8:2.2, 9:2.8}
TF_COEFF = {
    "1m":0.3, "3m":0.4, "5m":0.5, "15m":0.6, "30m":0.7,
    "1h":1.0, "2h":1.2, "4h":1.5, "6h":1.8, "8h":2.0, "12h":2.3,
    "1d":3.0, "3d":3.5, "1w":4.5, "1M":6.0
}

# --- CORAL TREND & SİNYAL FONKSİYONLARI ---
def coral_trend(close_list, sm=SM, cd=CD):
    if not close_list: return []
    di = (sm - 1)/2 + 1
    c1 = 2 / (di + 1); c2 = 1 - c1
    c3 = 3 * (cd**2 + cd**3)
    c4 = -3 * (2*cd**2 + cd + cd**3)
    c5 = 3*cd + 1 + cd**3 + 3*cd**2
    i1=[close_list[0]]; i2=i3=i4=i5=i6=[close_list[0]]
    i2=[i1[0]]; i3=[i2[0]]; i4=[i3[0]]; i5=[i4[0]]; i6=[i5[0]]
    for price in close_list[1:]:
        i1.append(c1*price + c2*i1[-1])
        i2.append(c1*i1[-1] + c2*i2[-1])
        i3.append(c1*i2[-1] + c2*i3[-1])
        i4.append(c1*i3[-1] + c2*i4[-1])
        i5.append(c1*i4[-1] + c2*i5[-1])
        i6.append(c1*i5[-1] + c2*i6[-1])
    return [-cd**3*i6[i]+c3*i5[i]+c4*i4[i]+c5*i3[i] for i in range(len(close_list))]

def count_close_mode_last(closes, highs, lows, bfr):
    HG = LW = None; last_high = last_low = None; last_index_HG = last_index_LW = None
    for i in range(len(closes)):
        close, high, low, trend = closes[i], highs[i], lows[i], bfr[i]
        if close > trend:
            if HG is None: HG=1; LW=None; last_high=high; last_index_HG=i
            elif high>last_high: HG=HG+1 if HG<9 else 1; last_high=high; last_index_HG=i
        elif close < trend:
            if LW is None: LW=1; HG=None; last_low=low; last_index_LW=i
            elif low<last_low: LW=LW+1 if LW<9 else 1; last_low=low; last_index_LW=i
    last_bar = len(closes)-1
    return (HG if last_index_HG==last_bar else 0, LW if last_index_LW==last_bar else 0)

def count_color_mode_last(bfr, highs, lows):
    HG = LW = last_color = None; last_high = last_low = None; last_index_HG = last_index_LW = None
    for i in range(1,len(bfr)):
        color = "green" if bfr[i]>bfr[i-1] else "red" if bfr[i]<bfr[i-1] else last_color
        high, low = highs[i], lows[i]
        if last_color is None or color != last_color:
            if color=="green": HG=1; LW=None; last_high=high; last_index_HG=i
            elif color=="red": LW=1; HG=None; last_low=low; last_index_LW=i
        else:
            if color=="green" and high>last_high: HG=HG+1 if HG<9 else 1; last_high=high; last_index_HG=i
            elif color=="red" and low<last_low: LW=LW+1 if LW<9 else 1; last_low=low; last_index_LW=i
        last_color=color
    last_bar=len(bfr)-1
    return (HG if last_index_HG==last_bar else 0, LW if last_index_LW==last_bar else 0)

def count_streaks_last(highs, lows):
    hg=lw=1; last_index_hg=last_index_lw=0
    for i in range(1,len(highs)):
        if highs[i]>highs[i-1]: hg+=1; last_index_hg=i
        else: hg=1; last_index_hg=i
        if lows[i]<lows[i-1]: lw+=1; last_index_lw=i
        else: lw=1; last_index_lw=i
    last_bar=len(highs)-1
    return (hg if last_index_hg==last_bar else 0, lw if last_index_lw==last_bar else 0)

def format_signal(x):
    if 1<=x<=9: return ["","①","②","⓷","④","⑤","⓺","⓻","⓼","⓽"][x]
    return "ⓧ"

# --- ASYNC VERİ ---
async def get_binance_data_async(session, symbol, interval):
    url=f"https://api.binance.com/api/v3/klines?symbol={symbol.upper()}&interval={interval}&limit={KLINE_LIMIT}"
    try:
        async with session.get(url, timeout=10) as resp:
            data = await resp.json()
            if not isinstance(data,list): return None
            closes=[float(x[4]) for x in data]; highs=[float(x[2]) for x in data]; lows=[float(x[3]) for x in data]
            return closes, highs, lows
    except Exception as e:
        logger.error(f"Binance veri hatası {symbol} {interval}: {e}")
        return None

async def get_current_price_async(session, symbol):
    url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol.upper()}"
    try:
        async with session.get(url, timeout=5) as resp:
            data = await resp.json()
            return data.get('price')
    except Exception as e:
        logger.error(f"Fiyat alma hatası {symbol}: {e}")
        return None

async def get_top_coins(limit=50):
    """Top coin'leri getir"""
    try:
        async with aiohttp.ClientSession() as session:
            url = "https://api.binance.com/api/v3/ticker/24hr"
            async with session.get(url, timeout=15) as resp:
                data = await resp.json()
        coins = []
        for d in data:
            symbol = d['symbol']
            if symbol.endswith("USDT") and not any(c in symbol for c in ["UP","DOWN","BULL","BEAR"]):
                try: coins.append((symbol.replace("USDT",""), float(d['quoteVolume'])))
                except: continue
        coins.sort(key=lambda x:x[1], reverse=True)
        return [c[0] for c in coins[:limit]]
    except Exception as e:
        logger.error(f"Top coins hatası: {e}")
        return ['BTC', 'ETH', 'BNB', 'SOL', 'XRP', 'ADA', 'AVAX', 'DOT', 'MATIC', 'LTC']

# --- ULTRAFILTER HESAPLAMA ---
async def compute_scores(coins):
    needed_tfs = set(TIMEFRAMES)
    for m in TIMEFRAMES: needed_tfs.update(SUB_TFS.get(m, []))
    data_map = {coin:{} for coin in coins}

    async with aiohttp.ClientSession() as session:
        tasks = []
        for coin in coins:
            for tf in needed_tfs:
                task = get_binance_data_async(session, coin + "USDT", tf)
                tasks.append((coin, tf, task))
        
        # Tüm task'leri çalıştır
        for coin, tf, task in tasks:
            data_map[coin][tf] = await task

    scores_list = []
    for coin in coins:
        main_scores = {}
        for main_tf in TIMEFRAMES:
            al_total = 0.0; sat_total = 0.0
            check_tfs = [main_tf] + SUB_TFS.get(main_tf, [])
            for tf in check_tfs:
                data = data_map[coin].get(tf)
                if not data: continue
                closes, highs, lows = data
                if not closes or len(closes)<6: continue
                bfr = coral_trend(closes)
                c_HG, c_LW = count_close_mode_last(closes, highs, lows, bfr)
                col_HG, col_LW = count_color_mode_last(bfr, highs, lows)
                s_HG, s_LW = count_streaks_last(highs, lows)
                for val in [c_LW, col_LW, s_LW]:
                    if val in SIGNAL_POWER: al_total += SIGNAL_POWER[val] * TF_COEFF.get(tf,1)
                for val in [c_HG, col_HG, s_HG]:
                    if val in SIGNAL_POWER: sat_total += SIGNAL_POWER[val] * TF_COEFF.get(tf,1)
            score = round(al_total - sat_total,1)
            main_scores[main_tf] = score
        total_score = round(sum(main_scores.values())/len(TIMEFRAMES),1)
        scores_list.append((coin, main_scores, total_score))
    return scores_list

# --- ANA TARAMA FONKSİYONU ---
async def run_ultrafilter_scan():
    """Ultrafilter analizini çalıştır"""
    logger.info("🔮 Ultra Filter analizi başlıyor...")
    
    coins = await get_top_coins(30)
    if not coins:
        logger.error("❌ Coin listesi alınamadı")
        return None
    
    logger.info(f"📊 {len(coins)} coin analiz ediliyor...")
    
    scores_list = await compute_scores(coins)
    
    # En güçlü AL sinyallerini bul
    al_sorted = sorted(scores_list, key=lambda x:x[2], reverse=True)[:15]
    sat_sorted = sorted(scores_list, key=lambda x:x[2])[:15]
    
    return al_sorted, sat_sorted

async def run_tara_scan():
    """Tara scan çalıştır"""
    logger.info("✅ Tara scan başlıyor...")
    
    coins = await get_top_coins(20)
    results = []
    
    async with aiohttp.ClientSession() as session:
        tasks = []
        for coin in coins:
            for tf in TIMEFRAMES:
                task = get_binance_data_async(session, coin + "USDT", tf)
                tasks.append((coin, tf, task))
        
        # Tüm verileri al
        data_map = {}
        for coin, tf, task in tasks:
            if coin not in data_map:
                data_map[coin] = {}
            data_map[coin][tf] = await task
    
    # Sinyalleri kontrol et
    for coin in coins:
        row = {"symbol": coin}
        has_signal = False
        
        for tf in TIMEFRAMES:
            data = data_map[coin].get(tf)
            if not data:
                row[tf] = ""
                continue
                
            closes, highs, lows = data
            if len(closes) < 10:
                row[tf] = ""
                continue
                
            bfr = coral_trend(closes)
            c_HG, c_LW = count_close_mode_last(closes, highs, lows, bfr)
            col_HG, col_LW = count_color_mode_last(bfr, highs, lows)
            s_HG, s_LW = count_streaks_last(highs, lows)
            
            buy_signal = any(x in [6,7,8,9] for x in [c_LW, col_LW, s_LW])
            sell_signal = any(x in [6,7,8,9] for x in [c_HG, col_HG, s_HG])
            
            if buy_signal:
                row[tf] = "🟢"
                has_signal = True
            elif sell_signal:
                row[tf] = "🔴"
                has_signal = True
            else:
                row[tf] = ""
        
        if has_signal:
            results.append(row)
    
    return results

# --- SONUÇLARI KAYDET ---
def save_results(al_signals, sat_signals, tara_results):
    """Sonuçları dosyaya kaydet"""
    
    # UltraFilter sonuçları
    with open('ultrafilter_results.txt', 'w', encoding='utf-8') as f:
        f.write("🤖 ULTRA FILTER SONUÇLARI\n")
        f.write(f"⏰ Tarama Zamanı: {datetime.now()}\n")
        f.write("=" * 60 + "\n\n")
        
        f.write("🟢 EN GÜÇLÜ 10 AL SİNYALİ:\n")
        f.write("-" * 50 + "\n")
        f.write(f"{'Coin':<6} {'1h':<5} {'4h':<5} {'1d':<5} {'1w':<5} {'1M':<5} Total\n")
        f.write("-" * 50 + "\n")
        
        for coin, scores, total in al_signals[:10]:
            line = f"{coin:<6} "
            for tf in TIMEFRAMES:
                score = scores.get(tf, 0)
                line += f"{score:>5.1f} "
            line += f"{total:>5.1f}"
            f.write(line + "\n")
        
        f.write("\n\n🔴 EN GÜÇLÜ 10 SAT SİNYALİ:\n")
        f.write("-" * 50 + "\n")
        for coin, scores, total in sat_signals[:10]:
            line = f"{coin:<6} "
            for tf in TIMEFRAMES:
                score = scores.get(tf, 0)
                line += f"{score:>5.1f} "
            line += f"{total:>5.1f}"
            f.write(line + "\n")
    
    # Tara sonuçları
    if tara_results:
        with open('tara_results.txt', 'w', encoding='utf-8') as f:
            f.write("✅ TARA SCAN SONUÇLARI\n")
            f.write(f"⏰ {datetime.now()}\n")
            f.write("=" * 40 + "\n")
            f.write(f"{'Coin':<7} {'1h':<3} {'4h':<3} {'1d':<3} {'1w':<3} {'1M':<3}\n")
            f.write("-" * 40 + "\n")
            
            for result in tara_results[:15]:
                line = f"{result['symbol']:<7} "
                for tf in TIMEFRAMES:
                    line += f"{result.get(tf, ''):<3} "
                f.write(line + "\n")

# --- ANA FONKSİYON ---
async def main():
    """GitHub Actions için ana fonksiyon"""
    logger.info("🚀 GitHub Actions Crypto Bot Başlıyor...")
    logger.info(f"⏰ Zaman: {datetime.now()}")
    
    try:
        # UltraFilter çalıştır
        ultrafilter_result = await run_ultrafilter_scan()
        
        if ultrafilter_result:
            al_signals, sat_signals = ultrafilter_result
            
            # Tara scan çalıştır
            tara_results = await run_tara_scan()
            
            # Sonuçları kaydet
            save_results(al_signals, sat_signals, tara_results)
            
            # Konsola özet göster
            print(f"\n🎯 ULTRA FILTER ÖZET:")
            print(f"🟢 AL Sinyalleri: {len(al_signals)} coin")
            print(f"🔴 SAT Sinyalleri: {len(sat_signals)} coin")
            
            if al_signals:
                print(f"\n🏆 EN İYİ 3 AL:")
                for coin, scores, total in al_signals[:3]:
                    print(f"   💰 {coin}: {total} puan")
            
            if tara_results:
                print(f"\n✅ TARA SONUÇLARI: {len(tara_results)} sinyal")
            
            logger.info("🎉 Tüm analizler başarıyla tamamlandı!")
            
        else:
            logger.error("❌ UltraFilter analizi başarısız")
        
    except Exception as e:
        logger.error(f"❌ Ana fonksiyon hatası: {e}")
        import traceback
        logger.error(traceback.format_exc())

if __name__ == "__main__":
    asyncio.run(main())
