import ccxt
import pandas as pd
import time
import logging
import requests
import threading
import json
import os
from flask import Flask, jsonify
from flask_cors import CORS
from datetime import datetime, time as dtime, timedelta

# ─────────────────────────────────────────────
# 📋 LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("bot_novadax.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 🔐 CONFIGURAÇÕES
# ─────────────────────────────────────────────
API_KEY    = os.environ.get('NOVADAX_API_KEY', 'a1948a5c-c851-4859-8c9d-5d2928e55cf3')
API_SECRET = os.environ.get('NOVADAX_API_SECRET', '0Y3wAeWThLnXWdtYfvHrqIBHNa9cD534')

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '8408182756:AAFcuwOazK89UAjQZERaqFiUQKjPKV1z0nQ')
TELEGRAM_CHATS = ['1998336872', '1898267356']  # Silas, Caique

# Pares em BRL — NovaDAX usa formato BTC/BRL
PARES = [
    'BTC/BRL', 'ETH/BRL', 'SOL/BRL', 'XRP/BRL',
    'AVAX/BRL', 'BNB/BRL', 'DOGE/BRL', 'OP/BRL',
    'JASMY/BRL', 'SUI/BRL', 'IMX/BRL', 'GRT/BRL',
]

# Pares de criptos antigas — vende se tiver lucro >= LUCRO_MINIMO
PARES_ANTIGOS        = ['GALA/BRL', 'AGI/BRL']
LUCRO_VENDA_ANTIGAS  = 0.10       # vende criptos não listadas se subirem 10%

TIMEFRAME            = '5m'
CAPITAL_BASE         = 25.0       # R$25 mínimo NovaDAX
RISCO_POR_TRADE      = 0.50       # 50% = R$25 por trade
MAX_POSICOES         = 2          # 2 slots × R$25 = R$50
MAX_POSICOES_ANTIGAS = 3          # GALA, WBX, AGI monitoradas separado
STOP_LOSS            = 0.020      # 2.0% — mais folgado pra dar tempo recuperar
TRAILING_STOP        = 0.015      # 1.5%
LUCRO_MINIMO_SAIDA   = 0.025      # 2.5% — cobre taxa 0.5% + lucro real ~R$0.62
LUCRO_MINIMO_VENDA   = 0.025      # 2.5%
LIMITE_PERDA_DIARIA  = 0.10       # 10% = R$5 máx perda/dia
LIMITE_TRANSACOES    = 999
QUEDA_RESERVA        = 999        # reserva desativada
STOPS_CONSECUTIVOS_MAX = 2        # pausa após 2 stops (capital menor = mais cuidado)
RSI_PERIODO          = 14
RSI_SOBRECOMPRADO    = 70         # mais conservador
RSI_SOBREVENDIDO     = 30
SCORE_MINIMO         = 4          # score mais alto = entradas mais seletivas
TAXA_ESTIMADA        = 0.005      # 0.5% NovaDAX

ARQUIVO_ESTADO    = 'estado_novadax.json'
ARQUIVO_HISTORICO = 'historico_novadax.json'

# ─────────────────────────────────────────────
# 🔌 Conectar na NovaDAX
# ─────────────────────────────────────────────
exchange = ccxt.novadax({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,
    'options': {
        'createMarketBuyOrderRequiresPrice': False,
    }
})

log.info("Conectando na NovaDAX (conta REAL)")

# ─────────────────────────────────────────────
# 📦 Estado global
# ─────────────────────────────────────────────
estado = {
    'posicoes':          {},
    'capital_inicial':   None,
    'capital_atual':     50.0,
    'saldo_brl':         0.0,
    'saldo_cripto':      0.0,
    'saldo_total':       0.0,
    'capital_reserva':   0.0,
    'reserva_usada':     False,
    'carteira_full':     {},  # todas as criptos da carteira em tempo real
    'perdas_dia':        0.0,
    'lucros_dia':        0.0,
    'transacoes_dia':    0,
    'wins_dia':          0,
    'losses_dia':        0,
    'stops_consecutivos': 0,
    'pausado_ate':       None,
    'stats_pares':       {},
    'stats_horarios':    {},
    'scores':            {},
    'pares_detalhes':    {},
    'sinal_atual':       'NEUTRO',
    'rsi_atual':         0.0,
    'preco_atual':       0.0,
    'ultimo_reset':      datetime.now().date().isoformat(),
    'relatorio_enviado': False,
    'status':            'iniciando',
    'ultimo_update':     datetime.now().isoformat(),
}

historico    = []
feed_eventos = []

def add_evento(tipo, msg):
    feed_eventos.append({
        'hora': datetime.now().strftime('%H:%M:%S'),
        'tipo': tipo,
        'msg':  msg,
    })
    if len(feed_eventos) > 100:
        feed_eventos.pop(0)

# ─────────────────────────────────────────────
# 🧠 Aprendizado
# ─────────────────────────────────────────────
def atualizar_stats(par, resultado, hora):
    if par not in estado['stats_pares']:
        estado['stats_pares'][par] = {'wins': 0, 'losses': 0, 'peso': 1.0}
    if resultado == 'win':
        estado['stats_pares'][par]['wins'] += 1
    else:
        estado['stats_pares'][par]['losses'] += 1
    wins   = estado['stats_pares'][par]['wins']
    losses = estado['stats_pares'][par]['losses']
    total  = wins + losses
    if total >= 5:
        estado['stats_pares'][par]['peso'] = round(0.5 + (wins / total), 2)

    hora_str = hora[:2]
    if par not in estado['stats_horarios']:
        estado['stats_horarios'][par] = {}
    if hora_str not in estado['stats_horarios'][par]:
        estado['stats_horarios'][par][hora_str] = {'wins': 0, 'losses': 0}
    if resultado == 'win':
        estado['stats_horarios'][par][hora_str]['wins'] += 1
    else:
        estado['stats_horarios'][par][hora_str]['losses'] += 1

def get_peso_par(par):
    if par not in estado['stats_pares']:
        return 1.0
    total = estado['stats_pares'][par]['wins'] + estado['stats_pares'][par]['losses']
    if total < 5:
        return 1.0
    return estado['stats_pares'][par]['peso']

def horario_favoravel(par):
    hora_str = datetime.now().strftime('%H')
    if par not in estado['stats_horarios']:
        return True, 'sem dados'
    if hora_str not in estado['stats_horarios'][par]:
        return True, 'hora nova'
    h = estado['stats_horarios'][par][hora_str]
    total = h['wins'] + h['losses']
    if total < 3:
        return True, 'poucos dados'
    winrate = h['wins'] / total
    if winrate < 0.35:
        return False, f"Win rate {winrate*100:.0f}% nessa hora"
    return True, f"Win rate {winrate*100:.0f}% nessa hora"

# ─────────────────────────────────────────────
# 💾 Persistência
# ─────────────────────────────────────────────
def salvar_estado():
    try:
        salvo = {k: v for k, v in estado.items() if k != 'pares_detalhes'}
        with open(ARQUIVO_ESTADO, 'w') as f:
            json.dump(salvo, f, indent=2, default=str)
    except Exception as e:
        log.error(f"Erro ao salvar estado: {e}")

def carregar_estado():
    try:
        if os.path.exists(ARQUIVO_ESTADO):
            with open(ARQUIVO_ESTADO, 'r') as f:
                salvo = json.load(f)
            campos = [
                'posicoes', 'capital_atual', 'capital_inicial', 'capital_reserva',
                'reserva_usada', 'transacoes_dia', 'wins_dia', 'losses_dia',
                'perdas_dia', 'lucros_dia', 'ultimo_reset', 'relatorio_enviado',
                'stops_consecutivos', 'pausado_ate', 'stats_pares', 'stats_horarios'
            ]
            for campo in campos:
                if campo in salvo:
                    estado[campo] = salvo[campo]
            log.info(f"Estado restaurado | {len(estado['posicoes'])} posicoes")
    except Exception as e:
        log.error(f"Erro ao carregar estado: {e}")

def salvar_historico():
    try:
        with open(ARQUIVO_HISTORICO, 'w') as f:
            json.dump(historico, f, indent=2, default=str)
    except Exception as e:
        log.error(f"Erro ao salvar historico: {e}")

def carregar_historico():
    global historico
    try:
        if os.path.exists(ARQUIVO_HISTORICO):
            with open(ARQUIVO_HISTORICO, 'r') as f:
                historico = json.load(f)
            log.info(f"Historico restaurado: {len(historico)} trades")
            for trade in historico:
                par  = trade.get('par', '')
                res  = trade.get('resultado', '')
                hora = trade.get('data', '00:00')[-5:]
                if par and res:
                    atualizar_stats(par, res, hora)
    except Exception as e:
        log.error(f"Erro ao carregar historico: {e}")

# ─────────────────────────────────────────────
# 🌐 Flask API
# ─────────────────────────────────────────────
app = Flask(__name__)
CORS(app, origins="*")

# Importa sistema de autenticação
from auth import registrar_rotas_auth, requer_auth, requer_admin

@app.after_request
def add_headers(response):
    response.headers['ngrok-skip-browser-warning'] = 'true'
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return response

@app.route('/status')
@requer_auth
def get_status():
    total   = estado['wins_dia'] + estado['losses_dia']
    winrate = (estado['wins_dia'] / total * 100) if total > 0 else 0

    pnl_total_usd = 0.0
    posicoes_info = {}
    for par, pos in estado['posicoes'].items():
        preco_atual = estado['pares_detalhes'].get(par, {}).get('preco', pos['preco_compra'])
        pnl_pct = (preco_atual - pos['preco_compra']) / pos['preco_compra'] * 100
        pnl_usd = (preco_atual - pos['preco_compra']) * pos['quantidade']
        pnl_total_usd += pnl_usd
        posicoes_info[par] = {
            'preco_compra': round(pos['preco_compra'], 4),
            'preco_atual':  round(preco_atual, 4),
            'preco_pico':   round(pos['preco_pico'], 4),
            'quantidade':   round(pos['quantidade'], 6),
            'pnl_pct':      round(pnl_pct, 2),
            'pnl_usd':      round(pnl_usd, 2),
            'total_invest': round(pos['preco_compra'] * pos['quantidade'], 2),
            'tipo':         pos.get('tipo', 'antigo'),
        }

    return jsonify({
        'status':            estado['status'],
        'exchange':          'NovaDAX',
        'moeda':             'BRL',
        'posicoes':          posicoes_info,
        'num_posicoes':      len(estado['posicoes']),
        'num_posicoes_novas': len([v for v in estado['posicoes'].values() if v.get('tipo') != 'antigo']),
        'num_posicoes_antigas': len([v for v in estado['posicoes'].values() if v.get('tipo') == 'antigo']),
        'max_posicoes':      MAX_POSICOES,
        'pnl_total_usd':     round(pnl_total_usd, 2),
        'pares_detalhes':    estado['pares_detalhes'],
        'pares_monitorados': PARES,
        'scores':            estado['scores'],
        'capital_inicial':   estado['capital_inicial'],
        'capital_atual':     round(estado['capital_atual'], 2),
        'saldo_brl':         round(estado['saldo_brl'], 2),
        'saldo_cripto':      round(estado['saldo_cripto'], 2),
        'saldo_total':       round(estado['saldo_total'], 2),
        'carteira_full':     estado.get('carteira_full', {}),
        'capital_reserva':   round(estado['capital_reserva'], 2),
        'reserva_usada':     estado['reserva_usada'],
        'lucros_dia':        round(estado['lucros_dia'], 2),
        'perdas_dia':        round(abs(estado['perdas_dia']), 2),
        'resultado_dia':     round(estado['lucros_dia'] - abs(estado['perdas_dia']), 2),
        'transacoes_dia':    estado['transacoes_dia'],
        'limite_transacoes': LIMITE_TRANSACOES,
        'wins_dia':          estado['wins_dia'],
        'losses_dia':        estado['losses_dia'],
        'winrate':           round(winrate, 1),
        'sinal_atual':       estado['sinal_atual'],
        'rsi_atual':         round(float(estado['rsi_atual']), 1) if estado['rsi_atual'] == estado['rsi_atual'] else 50.0,
        'preco_atual':       estado['preco_atual'],
        'ultimo_update':     estado['ultimo_update'],
        'stats_pares':       estado['stats_pares'],
        'stops_consecutivos': estado['stops_consecutivos'],
        'pausado_ate':       estado['pausado_ate'],
    })

@app.route('/historico')
@requer_auth
def get_historico():
    return jsonify(historico[-50:])

@app.route('/feed')
@requer_auth
def get_feed():
    return jsonify(list(reversed(feed_eventos[-50:])))

@app.route('/ngrok_url')
def get_ngrok_url():
    try:
        with open('ngrok_url.txt', 'r') as f:
            return jsonify({'url': f.read().strip()})
    except:
        return jsonify({'url': None})

def rodar_api():
    registrar_rotas_auth(app)

    @app.route('/bot/pausar', methods=['POST'])
    @requer_admin
    def pausar_bot():
        estado['status'] = 'pausado'
        add_evento('ALERTA', f"Bot pausado por {request.user['username']}")
        return jsonify({'success': True, 'status': 'pausado'})

    @app.route('/bot/retomar', methods=['POST'])
    @requer_admin
    def retomar_bot():
        estado['status'] = 'rodando'
        add_evento('INFO', f"Bot retomado por {request.user['username']}")
        return jsonify({'success': True, 'status': 'rodando'})

    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

# ─────────────────────────────────────────────
# 📨 Telegram
# ─────────────────────────────────────────────
def telegram(msg: str):
    for chat_id in TELEGRAM_CHATS:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            requests.post(url, json={
                'chat_id': chat_id,
                'text': msg,
                'parse_mode': 'HTML'
            }, timeout=10)
        except Exception as e:
            log.warning(f"Erro Telegram ({chat_id}): {e}")

# ─────────────────────────────────────────────
# 🔄 Reset diário
# ─────────────────────────────────────────────
def checar_reset_diario():
    hoje = datetime.now().date().isoformat()
    if hoje != estado['ultimo_reset']:
        estado['transacoes_dia']     = 0
        estado['perdas_dia']         = 0.0
        estado['lucros_dia']         = 0.0
        estado['wins_dia']           = 0
        estado['losses_dia']         = 0
        estado['reserva_usada']      = False
        estado['relatorio_enviado']  = False
        estado['stops_consecutivos'] = 0
        estado['pausado_ate']        = None
        estado['ultimo_reset']       = hoje
        estado['capital_reserva']    = estado['capital_atual'] * 0.20
        salvar_estado()
        log.info("Reset diario realizado")
        telegram(
            f"Novo dia iniciado - NovaDAX\n"
            f"Capital: R${estado['capital_atual']:.2f}\n"
            f"Reserva: R${estado['capital_reserva']:.2f}"
        )

def checar_relatorio_diario():
    agora = datetime.now().time()
    if dtime(23, 55) <= agora <= dtime(23, 59) and not estado['relatorio_enviado']:
        total     = estado['wins_dia'] + estado['losses_dia']
        winrate   = (estado['wins_dia'] / total * 100) if total > 0 else 0
        resultado = estado['lucros_dia'] - abs(estado['perdas_dia'])
        telegram(
            f"Relatorio NovaDAX\n\n"
            f"Resultado: R${resultado:+.2f}\n"
            f"Wins: {estado['wins_dia']} | Losses: {estado['losses_dia']}\n"
            f"Win Rate: {winrate:.1f}%\n"
            f"Transacoes: {estado['transacoes_dia']}"
        )
        estado['relatorio_enviado'] = True
        salvar_estado()

# ─────────────────────────────────────────────
# 🔍 Sincronizar posições + verificar criptos antigas
# ─────────────────────────────────────────────
def buscar_preco_compra_real(par, qtd_atual):
    """Busca o preço médio de compra real pelo histórico de ordens da NovaDAX"""
    try:
        # Busca últimas 100 ordens do par
        ordens = exchange.fetch_orders(par, limit=100)
        compras = [o for o in ordens if o['side'] == 'buy' and o['status'] == 'closed']

        if not compras:
            log.warning(f"{par}: sem histórico de compras — usando preço atual")
            return None

        # Calcula preço médio ponderado das últimas compras
        total_gasto  = 0.0
        total_comprado = 0.0
        for ordem in sorted(compras, key=lambda x: x['timestamp'], reverse=True):
            qtd_ordem   = float(ordem.get('filled', 0))
            preco_ordem = float(ordem.get('average') or ordem.get('price') or 0)
            if qtd_ordem <= 0 or preco_ordem <= 0:
                continue
            total_gasto    += qtd_ordem * preco_ordem
            total_comprado += qtd_ordem
            if total_comprado >= qtd_atual * 0.95:  # 95% da qtd atual coberta
                break

        if total_comprado > 0:
            preco_medio = total_gasto / total_comprado
            log.info(f"{par}: preço médio real de compra = R${preco_medio:.6f} (baseado em {total_comprado:.4f} unidades)")
            return preco_medio

    except Exception as e:
        log.warning(f"Erro ao buscar histórico {par}: {e}")
    return None

def sincronizar_posicao():
    try:
        balance    = exchange.fetch_balance()
        brl_livre  = float(balance['free'].get('BRL', 0))
        total_val  = brl_livre

        # Verifica posições salvas
        posicoes_validas = {}
        for par, pos in estado['posicoes'].items():
            moeda = par.split('/')[0]
            qtd   = float(balance['free'].get(moeda, 0))
            if qtd * pos['preco_compra'] > 1:
                ticker = exchange.fetch_ticker(par)
                total_val += qtd * ticker['last']
                posicoes_validas[par] = pos
                posicoes_validas[par]['quantidade'] = qtd
                log.info(f"Posicao restaurada: {par} | Compra: R${pos['preco_compra']:.4f}")
        estado['posicoes'] = posicoes_validas

        # Detecta TODAS as criptos da carteira automaticamente
        for moeda, qtd_info in balance['total'].items():
            if moeda == 'BRL':
                continue
            qtd = float(qtd_info or 0)
            if qtd <= 0:
                continue
            par = f"{moeda}/BRL"
            if par in estado['posicoes']:
                continue  # já tá monitorando
            try:
                ticker      = exchange.fetch_ticker(par)
                preco_atual = float(ticker['last'])
                valor       = qtd * preco_atual
                if valor < 0.5:
                    continue

                # Usa preço atual como referência (não altera P&L das antigas)
                preco_ref = preco_atual

                # Se tá na lista de trading → foi o bot que comprou → tipo novo
                # Se não tá na lista → cripto antiga do usuário → tipo antigo
                if par in PARES:
                    tipo = 'novo'
                else:
                    tipo = 'antigo'
                estado['posicoes'][par] = {
                    'preco_compra':      preco_ref,
                    'quantidade':        qtd,
                    'preco_pico':        preco_atual,
                    'tipo':              tipo,
                }
                total_val += valor
                log.info(f"Cripto detectada: {par} ({tipo}) | Qtd: {qtd} | Ref: R${preco_ref:.6f} | Valor: R${valor:.2f}")
            except:
                pass

        if estado['capital_inicial'] is None:
            estado['capital_inicial'] = brl_livre
            estado['capital_atual']   = brl_livre
            estado['capital_reserva'] = brl_livre * 0.20
        else:
            # Sincroniza capital_atual com saldo real BRL
            estado['capital_atual'] = brl_livre

        estado['saldo_brl']  = brl_livre
        estado['saldo_total'] = total_val
        estado['status']      = 'rodando'
        salvar_estado()

        log.info(f"Capital: R${estado['capital_atual']:.2f} | BRL livre: R${brl_livre:.2f} | Posicoes: {len(estado['posicoes'])}")
        telegram(
            f"Bot NovaDAX iniciado!\n"
            f"Capital: R${estado['capital_atual']:.2f}\n"
            f"BRL disponivel: R${brl_livre:.2f}\n"
            f"Reserva: R${estado['capital_reserva']:.2f}\n"
            f"Posicoes: {len(estado['posicoes'])}/{MAX_POSICOES}\n"
            f"Pares: {len(PARES)} monitorados\n"
            f"Timeframe: {TIMEFRAME} | CONTA REAL"
        )
    except Exception as e:
        log.error(f"Erro ao sincronizar: {e}")
        telegram(f"Erro ao iniciar bot: {e}")

# ─────────────────────────────────────────────
# 📊 Dados e indicadores
# ─────────────────────────────────────────────
def pegar_dados(par):
    ohlcv = exchange.fetch_ohlcv(par, timeframe=TIMEFRAME, limit=288)
    df = pd.DataFrame(ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
    df['time'] = pd.to_datetime(df['time'], unit='ms')
    return df

def calcular_rsi(df, periodo=RSI_PERIODO):
    delta = df['close'].diff()
    ganho = delta.clip(lower=0).rolling(periodo).mean()
    perda = (-delta.clip(upper=0)).rolling(periodo).mean()
    rs    = ganho / perda
    return 100 - (100 / (1 + rs))

def calcular_score(par):
    try:
        df = pegar_dados(par)
        df['ma9']    = df['close'].rolling(9).mean()
        df['ma21']   = df['close'].rolling(21).mean()
        df['ma50']   = df['close'].rolling(50).mean()
        df['rsi']    = calcular_rsi(df)
        df['vol_ma'] = df['volume'].rolling(20).mean()

        ma9_atual  = df['ma9'].iloc[-1]
        ma21_atual = df['ma21'].iloc[-1]
        ma9_prev   = df['ma9'].iloc[-2]
        ma21_prev  = df['ma21'].iloc[-2]
        ma50_atual = df['ma50'].iloc[-1]
        rsi_atual  = df['rsi'].iloc[-1]
        vol_atual  = df['volume'].iloc[-1]
        vol_media  = df['vol_ma'].iloc[-1]
        preco      = df['close'].iloc[-1]

        rsi_val = round(rsi_atual, 1) if not pd.isna(rsi_atual) else 50.0

        # Filtro tendência 1h
        tendencia_alta_1h = True
        try:
            df_1h = pd.DataFrame(
                exchange.fetch_ohlcv(par, timeframe='1h', limit=50),
                columns=['time', 'open', 'high', 'low', 'close', 'volume']
            )
            df_1h['ma50_1h'] = df_1h['close'].rolling(50).mean()
            tendencia_alta_1h = df_1h['close'].iloc[-1] > df_1h['ma50_1h'].iloc[-1]
        except:
            pass

        score   = 0
        sinal   = 'NEUTRO'
        motivos = []

        cruzou_para_cima = (ma9_prev <= ma21_prev) and (ma9_atual > ma21_atual)
        if cruzou_para_cima:
            score += 3
            sinal  = 'COMPRA'
            motivos.append('MA cruzou +3')
        else:
            motivos.append('MA sem cruzamento')

        if preco > ma50_atual:
            score += 2
            motivos.append('Acima MA50 +2')
        else:
            motivos.append('Abaixo MA50')

        if vol_atual >= vol_media * 1.0:
            score += 2
            motivos.append('Volume OK +2')
        else:
            motivos.append('Volume fraco')

        if 35 <= rsi_val <= 65:
            score += 2
            motivos.append('RSI ideal +2')
        elif rsi_val < 35:
            score += 1
            motivos.append('RSI sobrevendido +1')
        elif rsi_val > RSI_SOBRECOMPRADO:
            score -= 3
            sinal  = 'NEUTRO'
            motivos.append('RSI sobrecomprado -3')

        if sinal == 'COMPRA' and not tendencia_alta_1h:
            sinal = 'NEUTRO'
            score -= 2
            motivos.append('Tendencia 1h baixa -2')
        elif tendencia_alta_1h:
            motivos.append('Tendencia 1h OK')

        # Score dinâmico
        peso  = get_peso_par(par)
        if peso != 1.0:
            bonus = round((peso - 1.0) * 3)
            score += bonus
            if bonus != 0:
                motivos.append(f'Historico {bonus:+d}')

        # Horário
        favoravel, motivo_hora = horario_favoravel(par)
        if not favoravel and sinal == 'COMPRA':
            sinal = 'NEUTRO'
            score -= 2
            motivos.append(f'Hora ruim: {motivo_hora}')

        pico_24h  = df['high'].max()
        queda_24h = round((preco - pico_24h) / pico_24h * 100, 2)

        log.info(f"{par} | R${preco:.4f} | Score:{score} | RSI:{rsi_val} | {sinal}")
        add_evento('INFO', f"{par} | R${preco:.4f} | Score:{score} | RSI:{rsi_val} | {sinal} | {' · '.join(motivos)}")

        return {
            'par': par, 'score': score, 'sinal': sinal, 'rsi': rsi_val,
            'preco': round(preco, 6), 'ma9': round(ma9_atual, 6),
            'ma21': round(ma21_atual, 6), 'ma50': round(ma50_atual, 6),
            'volume_ok': bool(vol_atual >= vol_media * 1.0),
            'motivos': motivos, 'df': df,
            'queda_24h': queda_24h, 'pico_24h': round(pico_24h, 6),
        }
    except Exception as e:
        log.error(f"Erro score {par}: {e}")
        return {'par': par, 'score': -1, 'sinal': 'ERRO', 'rsi': 50.0,
                'preco': 0, 'ma9': 0, 'ma21': 0, 'ma50': 0,
                'volume_ok': False, 'motivos': ['Erro'], 'df': None,
                'queda_24h': 0, 'pico_24h': 0}

# ─────────────────────────────────────────────
# 🟢 Comprar
# ─────────────────────────────────────────────
def comprar(par, rsi):
    try:
        if par in estado['posicoes']:
            return
        if len(estado['posicoes']) >= MAX_POSICOES:
            return

        # Verifica saldo BRL disponível
        balance   = exchange.fetch_balance()
        brl_livre = float(balance['free'].get('BRL', 0))

        valor_op = max(estado['capital_atual'] * RISCO_POR_TRADE, CAPITAL_BASE)
        if brl_livre < valor_op:
            log.warning(f"Saldo BRL insuficiente: R${brl_livre:.2f} < R${valor_op:.2f}")
            return

        ticker     = exchange.fetch_ticker(par)
        preco      = ticker['last']
        quantidade = float(exchange.amount_to_precision(par, valor_op / preco))

        if quantidade <= 0:
            return

        # NovaDAX: passa o valor em BRL diretamente como amount
        order = exchange.create_market_buy_order(par, valor_op)
        preco_executado   = float(order.get('average') or order.get('price') or preco)
        quantidade_simulada = valor_op / preco_executado

        estado['posicoes'][par] = {
            'preco_compra': preco_executado,
            'quantidade':   quantidade_simulada,
            'preco_pico':   preco_executado,
            'tipo':         'novo',
        }
        estado['transacoes_dia'] += 1
        estado['saldo_brl']      -= valor_op
        salvar_estado()

        log.info(f"COMPROU {par} | R${preco_executado:.4f} | R${valor_op:.2f} | RSI:{rsi:.1f}")
        add_evento('COMPRA', f"{par} | R${preco_executado:.4f} | R${valor_op:.2f} | RSI:{rsi:.1f}")
        telegram(
            f"COMPRA - NovaDAX\n\n"
            f"Par: {par}\n"
            f"Preco: R${preco_executado:.4f}\n"
            f"Valor: R${valor_op:.2f}\n"
            f"RSI: {rsi:.1f}\n"
            f"Stop: R${preco_executado*(1-STOP_LOSS):.4f}\n"
            f"Lucro min: R${preco_executado*(1+LUCRO_MINIMO_SAIDA):.4f} (+{LUCRO_MINIMO_SAIDA*100:.0f}%)\n"
            f"Posicoes: {len(estado['posicoes'])}/{MAX_POSICOES}"
        )
    except ccxt.InsufficientFunds:
        log.error(f"Saldo insuficiente para comprar {par}")
    except Exception as e:
        log.error(f"Erro compra {par}: {e}")
        telegram(f"Erro ao comprar {par}: {e}")

# ─────────────────────────────────────────────
# 🏦 Comprar com reserva
# ─────────────────────────────────────────────
def comprar_reserva(par):
    try:
        if estado['reserva_usada'] or estado['capital_reserva'] <= 0:
            return

        balance   = exchange.fetch_balance()
        brl_livre = float(balance['free'].get('BRL', 0))
        if brl_livre < estado['capital_reserva']:
            return

        ticker     = exchange.fetch_ticker(par)
        preco      = ticker['last']
        valor_res  = estado['capital_reserva']
        quantidade = float(exchange.amount_to_precision(par, valor_res / preco))

        if quantidade <= 0:
            return

        # NovaDAX: passa o valor em BRL diretamente
        order = exchange.create_market_buy_order(par, valor_res)
        preco_executado   = float(order.get('average') or order.get('price') or preco)
        qtd_simulada      = valor_res / preco_executado

        if par in estado['posicoes']:
            pos         = estado['posicoes'][par]
            qtd_total   = pos['quantidade'] + qtd_simulada
            preco_medio = ((pos['preco_compra'] * pos['quantidade']) +
                           (preco_executado * qtd_simulada)) / qtd_total
            estado['posicoes'][par]['preco_compra'] = preco_medio
            estado['posicoes'][par]['quantidade']   = qtd_total
        else:
            estado['posicoes'][par] = {
                'preco_compra': preco_executado,
                'quantidade':   qtd_simulada,
                'preco_pico':   preco_executado,
                'tipo':         'reserva',
            }

        estado['reserva_usada']   = True
        estado['capital_reserva'] = 0.0
        salvar_estado()

        log.info(f"RESERVA {par} | R${preco_executado:.4f} | PM: R${estado['posicoes'][par]['preco_compra']:.4f}")
        add_evento('ALERTA', f"Reserva usada em {par} | R${preco_executado:.4f}")
        telegram(
            f"RESERVA ATIVADA - NovaDAX\n"
            f"Par: {par}\n"
            f"Queda de 10% detectada\n"
            f"Valor: R${valor_res:.2f}\n"
            f"Preco: R${preco_executado:.4f}\n"
            f"PM: R${estado['posicoes'][par]['preco_compra']:.4f}"
        )
    except Exception as e:
        log.error(f"Erro reserva {par}: {e}")

# ─────────────────────────────────────────────
# 📉 Checar queda brusca
# ─────────────────────────────────────────────
def checar_queda_brusca_todos():
    if estado['reserva_usada'] or estado['capital_reserva'] <= 0:
        return
    melhor_par   = None
    melhor_queda = 0.0
    for par, detalhes in estado['pares_detalhes'].items():
        queda = detalhes.get('queda_24h', 0)
        if queda <= -(QUEDA_RESERVA * 100):
            if queda < melhor_queda:
                melhor_queda = queda
                melhor_par   = par
    if melhor_par:
        add_evento('ALERTA', f"Queda 24h: {melhor_par} | {melhor_queda:.2f}%")
        comprar_reserva(melhor_par)

# ─────────────────────────────────────────────
# 🔴 Vender
# ─────────────────────────────────────────────
def vender(par, motivo="SINAL"):
    try:
        if par not in estado['posicoes']:
            return

        pos    = estado['posicoes'][par]
        moeda  = par.split('/')[0]
        ticker = exchange.fetch_ticker(par)
        preco  = ticker['last']

        # Quantidade real na exchange
        balance    = exchange.fetch_balance()
        saldo_real = float(balance['free'].get(moeda, 0))

        if saldo_real <= 0:
            log.warning(f"Saldo zero para {par} — removendo posicao")
            del estado['posicoes'][par]
            salvar_estado()
            return

        # Verifica quantidade mínima da NovaDAX
        try:
            market   = exchange.market(par)
            min_qtd  = float(market.get('limits', {}).get('amount', {}).get('min', 0))
            min_val  = float(market.get('limits', {}).get('cost', {}).get('min', 25))
            ticker   = exchange.fetch_ticker(par)
            val_venda = saldo_real * float(ticker['last'])

            if saldo_real < min_qtd or val_venda < min_val:
                log.warning(f"{par}: valor R${val_venda:.2f} abaixo do mínimo R${min_val:.2f} — nao vende")
                add_evento('INFO', f"{par}: valor abaixo do mínimo — mantendo posicao")
                del estado['posicoes'][par]  # remove do monitoramento
                salvar_estado()
                return
        except:
            pass

        quantidade_real = float(exchange.amount_to_precision(par, saldo_real))

        if quantidade_real <= 0:
            del estado['posicoes'][par]
            salvar_estado()
            return

        order           = exchange.create_market_sell_order(par, quantidade_real)
        preco_executado = float(order.get('average') or order.get('price') or preco)

        # P&L calculado com quantidade simulada
        qtd_sim       = pos['quantidade']
        lucro_pct     = (preco_executado - pos['preco_compra']) / pos['preco_compra'] * 100
        lucro_brl     = (preco_executado - pos['preco_compra']) * qtd_sim
        taxa          = (pos['preco_compra'] * qtd_sim) * TAXA_ESTIMADA
        lucro_liquido = lucro_brl - taxa

        historico.append({
            'data':         datetime.now().strftime('%d/%m %H:%M'),
            'par':          par,
            'tipo':         motivo,
            'preco_compra': round(pos['preco_compra'], 4),
            'preco_venda':  round(preco_executado, 4),
            'quantidade':   round(qtd_sim, 6),
            'pnl_pct':      round(lucro_pct, 2),
            'pnl_usd':      round(lucro_liquido, 2),
            'resultado':    'win' if lucro_liquido >= 0 else 'loss',
        })
        salvar_historico()

        del estado['posicoes'][par]
        estado['capital_atual'] += lucro_liquido

        # Reabastece reserva
        nova_reserva = estado['capital_atual'] * 0.20
        estado['capital_reserva'] = nova_reserva
        if not estado['posicoes']:
            estado['reserva_usada'] = False

        # Atualiza aprendizado
        hora_trade = datetime.now().strftime('%H:%M')
        atualizar_stats(par, 'win' if lucro_liquido >= 0 else 'loss', hora_trade)

        if lucro_liquido >= 0:
            estado['lucros_dia'] += lucro_liquido
            estado['wins_dia']   += 1
            estado['stops_consecutivos'] = 0
            emoji = "LUCRO"
        else:
            estado['perdas_dia'] += lucro_liquido
            estado['losses_dia'] += 1
            if motivo == "STOP LOSS":
                estado['stops_consecutivos'] += 1
                if estado['stops_consecutivos'] >= STOPS_CONSECUTIVOS_MAX:
                    pausado_ate = (datetime.now() + timedelta(hours=1)).isoformat()
                    estado['pausado_ate'] = pausado_ate
                    add_evento('ALERTA', f"3 stops seguidos — pausando 1h")
                    telegram(f"3 STOPS CONSECUTIVOS\nBot pausado por 1 hora")
            emoji = "PERDA"

        salvar_estado()

        log.info(f"VENDEU {par} [{motivo}] | R${preco_executado:.4f} | {lucro_pct:+.2f}% | Liq: R${lucro_liquido:+.2f}")
        add_evento('VENDA', f"{par} [{motivo}] | R${preco_executado:.4f} | {lucro_pct:+.2f}% | R${lucro_liquido:+.2f}")
        telegram(
            f"{emoji} - VENDA [{motivo}] - NovaDAX\n\n"
            f"Par: {par}\n"
            f"Preco: R${preco_executado:.4f}\n"
            f"P&L: {lucro_pct:+.2f}% (R${lucro_liquido:+.2f})\n"
            f"Taxa est.: R${taxa:.2f}\n"
            f"Capital: R${estado['capital_atual']:.2f}\n"
            f"Reserva: R${nova_reserva:.2f}\n"
            f"Wins: {estado['wins_dia']} | Losses: {estado['losses_dia']}"
        )
    except ccxt.InsufficientFunds:
        log.error(f"Saldo insuficiente para vender {par}")
    except Exception as e:
        log.error(f"Erro venda {par}: {e}")
        telegram(f"Erro ao vender {par}: {e}")

# ─────────────────────────────────────────────
# ⚠️ Checar risco
# ─────────────────────────────────────────────
def checar_risco(par):
    if par not in estado['posicoes']:
        return False

    pos    = estado['posicoes'][par]
    ticker = exchange.fetch_ticker(par)
    preco  = ticker['last']

    # Pega HIGH do candle
    try:
        candles    = exchange.fetch_ohlcv(par, timeframe=TIMEFRAME, limit=2)
        high_atual = candles[-1][2]
        preco_pico_candle = max(preco, high_atual)
    except:
        preco_pico_candle = preco

    if preco_pico_candle > pos['preco_pico']:
        estado['posicoes'][par]['preco_pico'] = preco_pico_candle

    lucro          = (preco - pos['preco_compra']) / pos['preco_compra']
    lucro_pico     = (pos['preco_pico'] - pos['preco_compra']) / pos['preco_compra']
    trailing_nivel = (pos['preco_pico'] - preco) / pos['preco_pico']

    # Stop Loss — NUNCA aplica em criptos avulsas ou antigas
    if pos.get('tipo') not in ('antigo', 'avulso'):
        if lucro <= -STOP_LOSS:
            add_evento('ALERTA', f"STOP LOSS {par} | {lucro*100:.2f}%")
            vender(par, motivo="STOP LOSS")
            return True

    # Trailing dinâmico — só pra trades normais
    if pos.get('tipo') not in ('antigo', 'avulso') and lucro_pico >= 0.005:
        if lucro_pico >= 0.04:
            trailing_din = 0.005
        elif lucro_pico >= 0.03:
            trailing_din = 0.008
        elif lucro_pico >= 0.02:
            trailing_din = 0.010
        else:
            trailing_din = TRAILING_STOP

        if trailing_nivel >= trailing_din:
            if lucro >= LUCRO_MINIMO_SAIDA:
                add_evento('ALERTA', f"TRAILING {par} | Pico: R${pos['preco_pico']:.4f} | {lucro*100:.2f}%")
                vender(par, motivo="TRAILING STOP")
                return True
            else:
                add_evento('INFO', f"Trailing aguardando {par} | {lucro*100:.2f}% < {LUCRO_MINIMO_SAIDA*100:.0f}%")

    # Criptos antigas (lista) — vende se lucro >= 2.5%
    if pos.get('tipo') == 'antigo' and lucro >= LUCRO_MINIMO_SAIDA:
        add_evento('ALERTA', f"CRIPTO ANTIGA {par} | Lucro {lucro*100:.2f}% — vendendo!")
        vender(par, motivo="CRIPTO ANTIGA LUCRATIVA")
        return True

    # Criptos avulsas — SOMENTE vende se subir 10%+, nunca stop loss
    if pos.get('tipo') == 'avulso' and lucro >= LUCRO_VENDA_ANTIGAS:
        add_evento('ALERTA', f"CRIPTO AVULSA {par} | +{lucro*100:.2f}% — meta 10% atingida!")
        telegram(
            f"VENDA AUTOMÁTICA\n"
            f"Par: {par}\n"
            f"Subiu {lucro*100:.2f}% — meta de 10% atingida!\n"
            f"Vendendo automaticamente"
        )
        vender(par, motivo="META 10% ATINGIDA")
        return True

    return False

# ─────────────────────────────────────────────
# 🚨 Limite de perda
# ─────────────────────────────────────────────
def limite_diario_atingido():
    if estado['capital_inicial'] is None:
        return False
    limite = estado['capital_inicial'] * LIMITE_PERDA_DIARIA
    if abs(estado['perdas_dia']) >= limite:
        telegram(f"LIMITE DE PERDA DIARIA\nPerda: R${abs(estado['perdas_dia']):.2f}\nBot encerrado.")
        return True
    return False

# ─────────────────────────────────────────────
# 🔁 LOOP PRINCIPAL
# ─────────────────────────────────────────────
def rodar_bot():
    log.info("Bot NovaDAX iniciando — CONTA REAL")
    carregar_estado()
    carregar_historico()
    sincronizar_posicao()

    while True:
        try:
            checar_reset_diario()
            checar_relatorio_diario()

            if limite_diario_atingido():
                for par in list(estado['posicoes'].keys()):
                    vender(par, motivo="LIMITE DIARIO")
                estado['status'] = 'encerrado'
                salvar_estado()
                break

            # Pausa por stops consecutivos
            if estado.get('pausado_ate'):
                agora = datetime.now().isoformat()
                if agora < estado['pausado_ate']:
                    restante = estado['pausado_ate'][11:16]
                    add_evento('ALERTA', f"Pausado ate {restante}")
                    time.sleep(300)
                    continue
                else:
                    estado['pausado_ate']        = None
                    estado['stops_consecutivos'] = 0
                    telegram("Pausa encerrada — retomando")

            estado['ultimo_update'] = datetime.now().isoformat()

            # Atualiza saldo real da carteira a cada ciclo — TODAS as criptos
            try:
                bal       = exchange.fetch_balance()
                brl_livre = float(bal['free'].get('BRL', 0))
                total_cripto  = 0.0
                carteira_full = {}  # todas as criptos com valor atual

                for moeda, qtd in bal['total'].items():
                    if moeda == 'BRL' or float(qtd or 0) <= 0:
                        continue
                    par = f"{moeda}/BRL"
                    try:
                        ticker = exchange.fetch_ticker(par)
                        preco  = float(ticker['last'])
                        valor  = float(qtd) * preco
                        if valor >= 0.01:  # ignora valores irrisórios
                            total_cripto += valor
                            carteira_full[moeda] = {
                                'quantidade': round(float(qtd), 8),
                                'preco':      round(preco, 6),
                                'valor_brl':  round(valor, 2),
                            }
                    except:
                        pass

                estado['saldo_brl']      = brl_livre
                estado['saldo_cripto']   = round(total_cripto, 2)
                estado['saldo_total']    = round(brl_livre + total_cripto, 2)
                estado['carteira_full']  = carteira_full
                log.info(f"Carteira: BRL R${brl_livre:.2f} + Cripto R${total_cripto:.2f} = Total R${estado['saldo_total']:.2f} | {list(carteira_full.keys())}")
            except Exception as e:
                log.warning(f"Erro ao atualizar saldo: {e}")

            # Monitora posições abertas
            for par in list(estado['posicoes'].keys()):
                try:
                    df    = pegar_dados(par)
                    preco = df['close'].iloc[-1]
                    pos   = estado['posicoes'][par]
                    pnl   = (preco - pos['preco_compra']) / pos['preco_compra'] * 100
                    pnl_brl = (preco - pos['preco_compra']) * pos['quantidade']

                    add_evento('INFO', f"Monit. {par} | R${preco:.4f} | P&L: {pnl:+.2f}% (R${pnl_brl:+.2f})")

                    ja_vendeu = checar_risco(par)

                    if not ja_vendeu:
                        resultado = calcular_score(par)
                        estado['pares_detalhes'][par] = {
                            'preco': resultado['preco'], 'score': resultado['score'],
                            'sinal': resultado['sinal'], 'rsi': resultado['rsi'],
                            'ma9': resultado['ma9'], 'ma21': resultado['ma21'],
                            'ma50': resultado['ma50'], 'volume_ok': bool(resultado['volume_ok']),
                            'motivos': resultado['motivos'],
                            'queda_24h': resultado.get('queda_24h', 0),
                            'pico_24h':  resultado.get('pico_24h', 0),
                        }
                        if resultado['sinal'] == 'VENDA':
                            lucro_atual = (preco - pos['preco_compra']) / pos['preco_compra']
                            if lucro_atual >= LUCRO_MINIMO_VENDA:
                                vender(par, motivo="SINAL MA")
                            else:
                                add_evento('INFO', f"Venda ignorada {par} | {lucro_atual*100:.2f}% < {LUCRO_MINIMO_VENDA*100:.0f}%")
                except Exception as e:
                    log.error(f"Erro monitorando {par}: {e}")

            # Checa queda brusca em todos
            checar_queda_brusca_todos()

            # Analisa pares sem posição
            # Conta só posições novas pra não bloquear os 5 slots com as antigas
            posicoes_novas = {k: v for k, v in estado['posicoes'].items() if v.get('tipo') != 'antigo'}
            pares_sem_posicao = [p for p in PARES if p not in estado['posicoes']]
            slots_disponiveis = MAX_POSICOES - len(posicoes_novas)

            if slots_disponiveis > 0 and pares_sem_posicao:
                resultados = []
                for par in pares_sem_posicao:
                    r = calcular_score(par)
                    resultados.append(r)
                    estado['scores'][par] = r['score']
                    estado['pares_detalhes'][par] = {
                        'preco': r['preco'], 'score': r['score'], 'sinal': r['sinal'],
                        'rsi': r['rsi'], 'ma9': r['ma9'], 'ma21': r['ma21'],
                        'ma50': r['ma50'], 'volume_ok': bool(r['volume_ok']),
                        'motivos': r['motivos'],
                        'queda_24h': r.get('queda_24h', 0),
                        'pico_24h':  r.get('pico_24h', 0),
                    }

                candidatos = [r for r in resultados if r['sinal'] == 'COMPRA' and r['score'] >= SCORE_MINIMO]
                candidatos.sort(key=lambda x: x['score'], reverse=True)

                for melhor in candidatos[:slots_disponiveis]:
                    # Verifica se tem BRL suficiente
                    bal = exchange.fetch_balance()
                    brl_livre = float(bal['free'].get('BRL', 0))

                    if brl_livre >= CAPITAL_BASE:
                        # Tem BRL — compra normalmente
                        estado['sinal_atual'] = f"COMPRA {melhor['par']}"
                        estado['rsi_atual']   = melhor['rsi']
                        estado['preco_atual'] = melhor['preco']
                        comprar(melhor['par'], melhor['rsi'])
                    else:
                        # Sem BRL — tenta rotação de capital
                        log.info(f"Sem BRL (R${brl_livre:.2f}) — verificando rotacao de capital")

                        # Busca posição com maior lucro >= 2.5%
                        melhor_venda = None
                        melhor_lucro = LUCRO_MINIMO_SAIDA  # mínimo pra considerar

                        for par_pos, pos in estado['posicoes'].items():
                            if pos.get('tipo') not in ('novo',):
                                continue  # só rotaciona posições de trading
                            try:
                                ticker     = exchange.fetch_ticker(par_pos)
                                preco_pos  = float(ticker['last'])
                                lucro_pos  = (preco_pos - pos['preco_compra']) / pos['preco_compra']
                                if lucro_pos > melhor_lucro:
                                    melhor_lucro = lucro_pos
                                    melhor_venda = par_pos
                            except:
                                pass

                        if melhor_venda:
                            log.info(f"Rotacao: vendendo {melhor_venda} (+{melhor_lucro*100:.2f}%) pra comprar {melhor['par']}")
                            add_evento('INFO', f"Rotacao de capital: {melhor_venda} → {melhor['par']}")
                            telegram(
                                f"ROTAÇÃO DE CAPITAL\n"
                                f"Vendendo: {melhor_venda} (+{melhor_lucro*100:.2f}%)\n"
                                f"Comprando: {melhor['par']} (Score: {melhor['score']})"
                            )
                            vender(melhor_venda, motivo="ROTACAO DE CAPITAL")
                            time.sleep(3)  # aguarda a venda processar
                            comprar(melhor['par'], melhor['rsi'])
                        else:
                            add_evento('INFO', f"Sinal {melhor['par']} ignorado — sem BRL e sem posicao lucrativa pra rodar")
                            log.info(f"Sem capital pra rodar — nenhuma posicao com lucro >= {LUCRO_MINIMO_SAIDA*100:.0f}%")

                if not candidatos and resultados:
                    melhor_score = max(resultados, key=lambda x: x['score'])
                    estado['sinal_atual'] = 'NEUTRO'
                    estado['rsi_atual']   = melhor_score['rsi']
                    estado['preco_atual'] = melhor_score['preco']

                # Atualiza saldo BRL
                try:
                    bal = exchange.fetch_balance()
                    estado['saldo_brl']  = float(bal['free'].get('BRL', 0))
                    estado['saldo_total'] = estado['saldo_brl']
                except:
                    pass

            time.sleep(300)  # 5 minutos

        except ccxt.NetworkError as e:
            log.warning(f"Rede: {e}")
            time.sleep(30)
        except ccxt.ExchangeError as e:
            log.error(f"Exchange: {e}")
            time.sleep(60)
        except KeyboardInterrupt:
            log.info("Bot encerrado")
            telegram("Bot NovaDAX encerrado manualmente")
            for par in list(estado['posicoes'].keys()):
                vender(par, motivo="ENCERRAMENTO MANUAL")
            salvar_estado()
            break
        except Exception as e:
            log.error(f"Erro: {e}")
            telegram(f"Erro: {e}")
            time.sleep(10)

if __name__ == '__main__':
    t = threading.Thread(target=rodar_api, daemon=True)
    t.start()
    rodar_bot()
