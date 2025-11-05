import sqlite3

# Nome do seu arquivo .db (ajuste se for diferente)
conexao = sqlite3.connect('orcamento.db')
cursor = conexao.cursor()

# Lista as tabelas existentes no banco
cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
tabelas = cursor.fetchall()
print("Tabelas disponíveis:", [t[0] for t in tabelas])

# Defina aqui o nome correto da sua tabela de transações
nome_tabela = input("Digite o nome da tabela onde deseja adicionar a coluna 'pago': ")

try:
    cursor.execute(f"ALTER TABLE {nome_tabela} ADD COLUMN pago TEXT DEFAULT 'Não'")
    conexao.commit()
    print(f"Coluna 'pago' adicionada com sucesso à tabela '{nome_tabela}'.")
except sqlite3.OperationalError as e:
    print("Erro:", e)

conexao.close()
