# 🚀 Guia de Deploy — Bot NovaDAX na Nuvem

## Opção 1 — Railway (RECOMENDADO — Grátis)

### Passo 1 — Criar conta
1. Acessa railway.app
2. Cria conta com GitHub

### Passo 2 — Criar projeto
1. Clica em "New Project"
2. "Deploy from GitHub repo"
3. Sobe os arquivos da pasta bot_cloud no GitHub
   (ou usa "Deploy from local" com Railway CLI)

### Passo 3 — Configurar variáveis de ambiente
No Railway, vá em "Variables" e adicione:
```
NOVADAX_API_KEY     = sua_api_key
NOVADAX_API_SECRET  = seu_api_secret
JWT_SECRET          = uma_chave_aleatoria_forte_aqui
TELEGRAM_TOKEN      = 8408182756:AAFcuwOazK89UAjQZERaqFiUQKjPKV1z0nQ
```

### Passo 4 — Deploy
Railway detecta o Procfile automaticamente e sobe o bot!

### Passo 5 — Acessar
1. Railway fornece uma URL pública: https://bot-novadax.railway.app
2. Abre o login.html no navegador
3. Coloca a URL do Railway
4. Login: silas / senha123 (troque depois!)

---

## Opção 2 — Render (Grátis)

1. Acessa render.com
2. "New Web Service"
3. Conecta GitHub com os arquivos
4. Build Command: `pip install -r requirements.txt`
5. Start Command: `python bot_novadax.py`
6. Adiciona as variáveis de ambiente
7. Deploy!

---

## Trocar senhas padrão

Após o primeiro login, acesse:
```
POST /auth/trocar-senha
{
  "senha_atual": "senha123",
  "senha_nova": "sua_senha_nova"
}
```

---

## Usuários padrão
| Usuário | Senha | Nível |
|---------|-------|-------|
| silas   | senha123 | Admin — controle total |
| caique  | senha456 | Viewer — só visualiza |
| roque   | senha789 | Admin — controle total |

⚠️ TROQUE AS SENHAS IMEDIATAMENTE APÓS O PRIMEIRO ACESSO!

---

## Arquivos necessários
```
bot_novadax.py       ← bot principal
auth.py              ← sistema de login
dashboard_novadax.html ← dashboard
login.html           ← página de login
requirements.txt     ← dependências
Procfile             ← comando de inicialização
railway.json         ← config Railway
.env.example         ← template de variáveis
```
