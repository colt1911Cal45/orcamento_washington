from flask import Flask, render_template, request, redirect, url_for, session, flash
import sqlite3
from datetime import datetime, date
from dateutil.relativedelta import relativedelta
from werkzeug.security import generate_password_hash, check_password_hash
import pytesseract
from PIL import Image
import io
import os
import re
from werkzeug.utils import secure_filename

# ====== NOVO: PDF ======
import fitz  # PyMuPDF

# ====== Opcional: OpenCV (melhora OCR se disponível) ======
try:
    import cv2
except Exception:
    cv2 = None

# =========================
# 🔧 Configurações básicas
# =========================
# Ajuste se o Tesseract estiver em outro caminho
# pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

app = Flask(__name__)
from dotenv import load_dotenv
load_dotenv()
import os
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'fallback_inseguro')


# 📂 Pasta para uploads (imagem/PDF)
UPLOAD_FOLDER = 'static/uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# =========================
# 🔹 Banco de Dados
# =========================
def conectar_db():
    conn = sqlite3.connect('orcamento.db')
    conn.row_factory = sqlite3.Row
    return conn

# =========================
# 🔹 Filtros/Helpers Jinja
# =========================
@app.template_filter('currency')
def currency_format(valor):
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

@app.template_filter('datetime_obj')
def datetime_obj(value):
    return datetime.strptime(value, '%Y-%m-%d')

@app.context_processor
def inject_now():
    return {'now': datetime.now}

def parse_valor_br(valor_str: str) -> float:
    if valor_str is None:
        raise ValueError("valor vazio")
    s = str(valor_str).strip()
    if not s:
        raise ValueError("valor vazio")
    s = s.replace("R$", "").replace("r$", "").replace(" ", "")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    return float(s)

# =========================
# 🔹 Helpers OCR / PDF
# =========================
MESES_PT = {
    "jan": 1, "janeiro": 1, "fev": 2, "fevereiro": 2, "mar": 3, "março": 3, "marco": 3,
    "abr": 4, "abril": 4, "mai": 5, "maio": 5, "jun": 6, "junho": 6, "jul": 7, "julho": 7,
    "ago": 8, "agosto": 8, "set": 9, "setembro": 9, "out": 10, "outubro": 10,
    "nov": 11, "novembro": 11, "dez": 12, "dezembro": 12,
}

def preprocessar_imagem_opcional(path: str) -> str:
    """Se OpenCV existir, gera uma cópia binarizada para melhorar OCR."""
    if not cv2:
        return path
    img = cv2.imread(path)
    if img is None:
        return path
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.bilateralFilter(gray, 9, 75, 75)
    thr = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    out = path.rsplit(".", 1)[0] + "_proc.jpg"
    cv2.imwrite(out, thr)
    return out

def normalizar_valor_br(p: str) -> str:
    s = p.strip().replace("R$", "").replace(" ", "")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    return s

def extrair_valor(texto: str) -> str:
    padroes = [
        r"R\$\s*([\d\.\,]+)",
        r"valor(?:\s*(?:total|pago|do\s*pagamento|a\s*pagar))?[:\s]*([\d\.\,]+)",
        r"pix.*?([\d\.\,]+)",
        r"\b([\d]{1,3}(?:\.[\d]{3})+,\d{2})\b",
        r"\b(\d+,\d{2})\b",
    ]
    for rx in padroes:
        m = re.search(rx, texto, flags=re.IGNORECASE|re.DOTALL)
        if m:
            try:
                return normalizar_valor_br(m.group(1))
            except Exception:
                pass
    return ""

def _try_parse_date(fmt: str, s: str) -> str:
    try:
        return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
    except Exception:
        return ""

def extrair_data(texto: str) -> str:
    # 1) dd/mm/aaaa ou dd/mm/aa
    m = re.search(r"\b(\d{2}/\d{2}/\d{2,4})\b", texto)
    if m:
        s = m.group(1)
        for fmt in ("%d/%m/%Y", "%d/%m/%y"):
            iso = _try_parse_date(fmt, s)
            if iso:
                return iso
    # 2) 25 Out, 2025 | 25 Out 2025 | 25 de Outubro de 2025
    m = re.search(r"\b(\d{1,2})\s*(?:de\s*)?([A-Za-zçÇáÁéÉíÍóÓúÚãõÃÕ]+)[\s,/-]+(\d{2,4})\b",
                  texto, flags=re.IGNORECASE)
    if m:
        dia = int(m.group(1)); mes_txt = m.group(2).lower(); ano = int(m.group(3))
        mes = MESES_PT.get(mes_txt[:3], MESES_PT.get(mes_txt, 0))
        if ano < 100: ano = 2000 + ano
        if 1 <= dia <= 31 and 1 <= mes <= 12:
            try:
                return date(ano, mes, dia).strftime("%Y-%m-%d")
            except Exception:
                pass
    return ""

CATEGORIAS_PALAVRAS = [
    ({"posto", "combust"}, "Combustível"),
    ({"hotel", "hosped"}, "Hospedagem"),
    ({"churrasc", "restaur", "lanche", "almoço", "jantar"}, "Alimentação"),
    ({"farm", "dercos", "vichy", "nivea", "remédio", "medic"}, "Farmácia"),
    ({"mercado", "supermercado"}, "Alimentação"),
    ({"energia", "luz", "coelba"}, "Energia"),
    ({"água", "agua", "embasa"}, "Água"),
    ({"mensalidade", "diária", "diarias", "escola", "reforço", "arco-iris"}, "Educação"),
    ({"uber", "99app", "transporte"}, "Transporte"),
]

def sugerir_categoria(texto: str, default="Desconhecida") -> str:
    low = texto.lower()
    for palavras, cat in CATEGORIAS_PALAVRAS:
        if any(p in low for p in palavras):
            return cat
    return default

def sugerir_descricao(texto: str) -> str:
    linhas = [l.strip() for l in texto.splitlines()]
    linhas = [l for l in linhas if len(l) >= 4 and not l.lower().startswith(("comprovante", "documento", "via", "@"))]
    return (linhas[0][:60] if linhas else "Detectado via arquivo")

def is_pdf(filename: str) -> bool:
    return filename.lower().endswith(".pdf")

def texto_de_pdf(pdf_path: str) -> str:
    """
    Lê PDF com PyMuPDF:
    1) tenta texto incorporado (get_text)
    2) se vier muito curto, renderiza a página em imagem e aplica OCR
    """
    doc = fitz.open(pdf_path)
    partes = []
    for page in doc:
        texto = page.get_text("text") or ""
        if len(texto.strip()) < 30:
            # fallback OCR por página
            pix = page.get_pixmap(dpi=300, alpha=False)
            img = Image.open(io.BytesIO(pix.tobytes("jpeg")))
            partes.append(pytesseract.image_to_string(img))
        else:
            partes.append(texto)
    doc.close()
    return "\n".join(partes)

# =========================
# 🔹 Página inicial (lista + filtros)
# =========================
@app.route('/')
def index():
    if 'usuario' not in session:
        return redirect(url_for('login'))

    mes = request.args.get('mes', 'Todos')
    ano = request.args.get('ano', 'Todos')
    categoria = request.args.get('categoria')
    tipo = request.args.get('tipo')
    valor_min = request.args.get('valor_min')
    valor_max = request.args.get('valor_max')
    usuario = request.args.get('usuario')

    query = 'SELECT * FROM Transacoes WHERE 1=1'
    params = []

    if mes != 'Todos' and ano != 'Todos':
        data_inicial = f'{ano}-{mes}-01'
        data_final = f'{ano}-{mes}-31'
        query += ' AND data BETWEEN ? AND ?'
        params.extend([data_inicial, data_final])
    elif mes != 'Todos':
        query += " AND strftime('%m', data) = ?"
        params.append(mes)
    elif ano != 'Todos':
        query += " AND strftime('%Y', data) = ?"
        params.append(ano)

    if categoria:
        query += ' AND categoria = ?'
        params.append(categoria)

    if tipo:
        query += ' AND tipo = ?'
        params.append(tipo)

    if valor_min:
        try:
            params.append(parse_valor_br(valor_min))
            query += ' AND valor >= ?'
        except ValueError:
            flash('Valor mínimo inválido (ex: 1234,56).')

    if valor_max:
        try:
            params.append(parse_valor_br(valor_max))
            query += ' AND valor <= ?'
        except ValueError:
            flash('Valor máximo inválido (ex: 1234,56).')

    if usuario:
        query += ' AND usuario = ?'
        params.append(usuario)

    query += ' ORDER BY data DESC'

    conn = conectar_db()
    cursor = conn.cursor()
    cursor.execute(query, params)
    linhas = cursor.fetchall()
    transacoes = [dict(linha) for linha in linhas]
    conn.close()

    return render_template('index.html', transacoes=transacoes, mes=mes, ano=ano, usuario=usuario)

# =========================
# 🔹 Adicionar nova transação
# =========================
@app.route('/adicionar', methods=['GET', 'POST'])
def adicionar():
    if 'usuario' not in session:
        return redirect(url_for('login'))

    if request.method == 'GET':
        return render_template('adicionar.html')

    # POST
    conn = conectar_db()
    cursor = conn.cursor()

    descricao = request.form.get('descricao', '').strip()
    valor_str = request.form.get('valor', '').strip()

    if not descricao:
        flash('Informe a descrição.')
        return redirect(request.referrer or url_for('index'))

    try:
        valor_total = parse_valor_br(valor_str)
    except ValueError:
        flash('Erro: Valor não detectado ou inválido. Ex: 1234,56')
        return redirect(request.referrer or url_for('index'))

    categoria = request.form.get('categoria', '').strip()
    tipo = request.form.get('tipo', '').strip()
    data = request.form.get('data', '').strip()
    parcelas = int(request.form.get('parcelas', 1) or 1)
    pago = request.form.get('pago', 'Não')
    usuario = session.get('usuario', 'Desconhecido')

    if not data:
        flash('Informe a data.')
        return redirect(request.referrer or url_for('index'))

    valor_parcela = round(valor_total / parcelas, 2)

    for i in range(parcelas):
        data_parcela = datetime.strptime(data, '%Y-%m-%d') + relativedelta(months=i)
        data_formatada = data_parcela.strftime('%Y-%m-%d')
        descricao_parcelada = f"{descricao} ({i+1}/{parcelas})" if parcelas > 1 else descricao

        cursor.execute('''
            INSERT INTO Transacoes (descricao, valor, categoria, tipo, data, pago, usuario)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (descricao_parcelada, valor_parcela, categoria, tipo, data_formatada, pago, usuario))

    conn.commit()
    conn.close()
    return redirect(request.referrer or url_for('index'))

# =========================
# 🔹 Editar / Excluir
# =========================
@app.route('/editar/<int:id>', methods=['GET', 'POST'])
def editar(id):
    if 'usuario' not in session:
        return redirect(url_for('login'))

    conn = conectar_db()
    cursor = conn.cursor()

    if request.method == 'POST':
        descricao = request.form.get('descricao', '').strip()
        valor_str = request.form.get('valor', '').strip()
        categoria = request.form.get('categoria', '').strip()
        tipo = request.form.get('tipo', '').strip()
        data = request.form.get('data', '').strip()
        pago = request.form.get('pago', 'Não')
        usuario = session.get('usuario', 'Não informado')

        if not descricao:
            flash('Informe a descrição.'); return redirect(request.referrer or url_for('index'))
        try:
            valor = parse_valor_br(valor_str)
        except ValueError:
            flash('Erro: Valor inválido. Ex: 1234,56'); return redirect(request.referrer or url_for('index'))
        if not data:
            flash('Informe a data.'); return redirect(request.referrer or url_for('index'))

        cursor.execute('''
            UPDATE Transacoes
            SET descricao = ?, valor = ?, categoria = ?, tipo = ?, data = ?, pago = ?, usuario = ?
            WHERE id = ?
        ''', (descricao, valor, categoria, tipo, data, pago, usuario, id))

        conn.commit()
        conn.close()
        return redirect(request.referrer or url_for('index'))

    cursor.execute('SELECT * FROM Transacoes WHERE id = ?', (id,))
    transacao = cursor.fetchone()
    conn.close()
    return render_template('editar.html', transacao=transacao)

@app.route('/excluir/<int:id>')
def excluir(id):
    if 'usuario' not in session:
        return redirect(url_for('login'))
    conn = conectar_db()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM Transacoes WHERE id = ?', (id,))
    conn.commit()
    conn.close()
    return redirect(request.referrer or url_for('index'))

# =========================
# 🔹 Upload + Extração de ARQUIVO (Imagem ou PDF)
# =========================
@app.route('/extrair', methods=['GET', 'POST'])
def extrair_dados():
    if 'usuario' not in session:
        return redirect(url_for('login'))

    dados_extraidos = {}

    if request.method == 'POST':
        arquivo = request.files.get('imagem')  # mantém nome do campo no template
        if not arquivo:
            flash('Selecione um arquivo (imagem ou PDF).')
            return redirect(url_for('extrair_dados'))

        caminho = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(arquivo.filename))
        arquivo.save(caminho)

        # --- Se for PDF: usa PyMuPDF (texto embutido) e fallback OCR por página
        if is_pdf(caminho):
            texto = texto_de_pdf(caminho)
        else:
            # Imagem: faz pré-processamento se possível e OCR
            caminho_proc = preprocessar_imagem_opcional(caminho)
            texto = pytesseract.image_to_string(Image.open(caminho_proc))

        # Extrações robustas
        valor_detectado = extrair_valor(texto)
        data_detectada  = extrair_data(texto)
        categoria       = sugerir_categoria(texto)
        descricao       = sugerir_descricao(texto)

        dados_extraidos = {
            "descricao": descricao,
            "valor": valor_detectado,
            "data": data_detectada,
            "tipo": "Despesa",
            "categoria": categoria,
            "ocr_preview": texto[:1200],
        }

    return render_template('extrair.html', dados=dados_extraidos)

# =========================
# 🔹 Login / Registro / Logout
# =========================
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        usuario = request.form.get('usuario', '').strip()
        senha = request.form.get('senha', '')

        conn = conectar_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM Usuarios WHERE usuario = ?", (usuario,))
        user = cursor.fetchone()
        conn.close()

        if user and check_password_hash(user['senha'], senha):
            session['usuario'] = user['usuario']
            flash('Login realizado com sucesso!')
            return redirect(url_for('index'))
        else:
            flash('Usuário ou senha inválidos')
    return render_template('login.html')

@app.route('/registrar', methods=['GET', 'POST'])
def registrar():
    if request.method == 'POST':
        nome = request.form.get('nome', '').strip()
        usuario = request.form.get('usuario', '').strip()
        senha_raw = request.form.get('senha', '')
        if not nome or not usuario or not senha_raw:
            flash('Preencha todos os campos.'); return redirect(url_for('registrar'))

        senha = generate_password_hash(senha_raw)
        conn = conectar_db()
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO Usuarios (nome, usuario, senha) VALUES (?, ?, ?)", (nome, usuario, senha))
            conn.commit()
            flash('Usuário criado com sucesso! Faça login.')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('Erro: usuário já existe.')
        finally:
            conn.close()
    return render_template('registrar.html')

@app.route('/logout')
def logout():
    session.pop('usuario', None)
    flash('Você saiu do sistema.')
    return redirect(url_for('login'))

# =========================
# 🔹 Run
# =========================
if __name__ == '__main__':
    # acesse em http://localhost:3000/
    app.run(host='0.0.0.0', port=3000, debug=True)

