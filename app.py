from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from flask import Flask, jsonify, render_template, render_template_string, request

BASE_DIR = Path(__file__).parent
PADRON_DIR = BASE_DIR
ORDERS_FILE = BASE_DIR / "orders.json"

# Marcas y día de llegada por orden de compra.
# Si la marca no está aquí, se marca como "Encargar a Montevideo".
BRAND_ARRIVAL = {
    "SUPRA": "Llega por orden de compra",
    "BELSIR": "Llega por orden de compra (día 2)",
    "USA PET": "Llega por orden de compra (día 3)",
    "DISTRICO": "Llega por orden de compra (día 3)",
    "DASLICAR": "Llega por orden de compra (día 5)",
    "FARMINA": "Llega por orden de compra (día 6)",
    "SADENIR": "Llega por orden de compra (día 4)",
}


def find_padron_file() -> Path | None:
    # Buscar el primer CSV en la carpeta
    csvs = sorted(PADRON_DIR.glob("*.csv"))
    return csvs[0] if csvs else None


def load_padron():
    path = find_padron_file()
    if not path:
        return []

    rows = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            dialect = csv.Sniffer().sniff(f.read(2048))
            f.seek(0)
            reader = csv.reader(f, dialect)
            data = list(reader)
    except Exception:
        # fallback con punto y coma
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f, delimiter=";")
            data = list(reader)

    # Identificar la fila de encabezados que contiene "Código" y "Nombre"
    header_idx = None
    for i, row in enumerate(data):
        if "Código" in row and "Nombre" in row:
            header_idx = i
            headers = row
            break
    if header_idx is None:
        return []

    records = []
    for row in data[header_idx + 1 :]:
        if not any(row):
            continue
        rec = dict(zip(headers, row))
        records.append(
            {
                "codigo": rec.get("Código", "").strip(),
                "codigo_barra": rec.get("Código de Barras", "").strip(),
                "nombre": rec.get("Nombre", "").strip(),
                "fabricante": rec.get("Fabricante", "").strip(),
                "marca": rec.get("Marca", "").strip(),
                "tipo": rec.get("Tipo Producto", "").strip(),
            }
        )
    return records


PADRON = load_padron()


def load_orders():
    if ORDERS_FILE.exists():
        try:
            with ORDERS_FILE.open("r", encoding="utf-8") as f:
                raw = json.load(f)
                # normalizar campos faltantes por compatibilidad
                normalized = []
                for o in raw:
                    o.setdefault("estado", "pendiente")
                    o.setdefault("fecha_llegada", None)
                    o.setdefault("fecha_aviso", None)
                    o.setdefault("fecha_entrega", None)
                    o.setdefault("firma", None)
                    normalized.append(o)
                return normalized
        except json.JSONDecodeError:
            return []
    return []


def save_orders(orders):
    with ORDERS_FILE.open("w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=2)


def arrival_info(brand: str) -> str:
    key = (brand or "").strip().upper()
    return BRAND_ARRIVAL.get(key, "Encargar a Montevideo")


def filter_products(query: str, limit: int = 20):
    q = query.strip().upper()
    if not q:
        return []
    results = []
    for item in PADRON:
        text = " ".join(
            [
                item.get("codigo", ""),
                item.get("codigo_barra", ""),
                item.get("nombre", ""),
                item.get("marca", ""),
            ]
        ).upper()
        if q in text:
            result = item.copy()
            result["via"] = arrival_info(item.get("marca", ""))
            results.append(result)
        if len(results) >= limit:
            break
    return results


def create_order(payload):
    product_code = payload.get("codigo", "").strip()
    sucursal = payload.get("sucursal", "").strip()
    cantidad = payload.get("cantidad", 1)
    obs = payload.get("observaciones", "").strip()

    product = next((p for p in PADRON if p.get("codigo") == product_code), None)
    if not product:
        raise ValueError("Producto no encontrado en padrón")

    order = {
        "id": str(uuid4()),
        "fecha_solicitud": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sucursal": sucursal or "N/D",
        "producto": {
            "codigo": product.get("codigo"),
            "nombre": product.get("nombre"),
            "marca": product.get("marca"),
            "via": arrival_info(product.get("marca")),
        },
        "cantidad": cantidad,
        "estado": "pendiente",
        "fecha_llegada": None,
        "fecha_aviso": None,
        "fecha_entrega": None,
        "firma": None,
        "observaciones": obs,
    }
    orders = load_orders()
    orders.insert(0, order)
    save_orders(orders)
    return order


def update_order_status(order_id: str, action: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    orders = load_orders()
    target = None
    for o in orders:
        if o.get("id") == order_id:
            target = o
            break
    if not target:
        raise ValueError("Pedido no encontrado")

    current = target.get("estado", "pendiente")
    if action == "llego":
        if current != "pendiente":
            raise ValueError("Secuencia inválida: ya no está pendiente")
        target["estado"] = "llegado"
        target["fecha_llegada"] = now
    elif action == "avisado":
        if current not in ("llegado", "avisado"):
            raise ValueError("Primero marca como 'Llegó'")
        target["estado"] = "avisado"
        target["fecha_aviso"] = now
    elif action == "entregado":
        if current not in ("avisado", "entregado"):
            raise ValueError("Primero marca como 'Avisado'")
        if not target.get("firma"):
            raise ValueError("Captura la firma antes de entregar")
        target["estado"] = "entregado"
        target["fecha_entrega"] = now
    else:
        raise ValueError("Acción no válida")

    save_orders(orders)
    return target


def save_signature(order_id: str, data_url: str):
    orders = load_orders()
    target = None
    for o in orders:
        if o.get("id") == order_id:
            target = o
            break
    if not target:
        raise ValueError("Pedido no encontrado")
    target["firma"] = data_url
    save_orders(orders)
    return target


app = Flask(__name__)


@app.route("/")
def index():
    return render_template_string(
        """
        <!doctype html>
        <html lang="es">
        <head>
            <meta charset="utf-8" />
            <meta name="viewport" content="width=device-width, initial-scale=1" />
            <title>Pedidos de comidas</title>
            <style>
                :root {
                    --bg: linear-gradient(135deg, #0c1f3f 0%, #143e75 50%, #1b4f9a 100%);
                    --card: rgba(255,255,255,0.92);
                    --accent: #1b4f9a;
                    --accent-2: #f6c344;
                    --text: #0f233d;
                    --muted: #4a6079;
                    --danger: #e94848;
                    --success: #1f9d55;
                }
                * { box-sizing: border-box; }
                body {
                    margin: 0;
                    min-height: 100vh;
                    background: var(--bg);
                    color: var(--text);
                    font-family: "Segoe UI", Arial, sans-serif;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    padding: 20px 28px;
                }
                .layout { width: min(1200px, 100%); }
                .grid { display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); align-items: start; }
                .card {
                    background: var(--card);
                    border: 1px solid rgba(0,0,0,0.06);
                    border-radius: 16px;
                    padding: 22px;
                    box-shadow: 0 20px 60px rgba(0,0,0,0.20);
                    margin-bottom: 14px;
                }
                h1 { margin: 0 0 10px; font-size: 30px; }
                .subtitle { color: var(--muted); margin: 0 0 16px; }
                label { display:block; margin: 8px 0 4px; font-weight: 600; }
                input, select, textarea {
                    width: 100%;
                    padding: 10px 12px;
                    border-radius: 10px;
                    border: 1px solid rgba(0,0,0,0.08);
                    background: rgba(0,0,0,0.03);
                    color: var(--text);
                    font-size: 15px;
                }
                button {
                    padding: 12px 16px;
                    border-radius: 10px;
                    border: none;
                    background: linear-gradient(135deg, var(--accent), var(--accent-2));
                    color: #0f172a;
                    cursor: pointer;
                    font-weight: 700;
                    letter-spacing: 0.2px;
                    transition: transform 0.1s ease, box-shadow 0.2s ease;
                }
                button:hover { transform: translateY(-1px); box-shadow: 0 10px 20px rgba(0,0,0,0.2); }
                .row { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; }
                .result-item { padding: 10px; border-radius: 10px; border:1px solid rgba(0,0,0,0.06); cursor:pointer; background: rgba(0,0,0,0.02); }
                .result-item:hover { border-color: var(--accent); box-shadow: 0 6px 16px rgba(0,0,0,0.08); }
                .tag { padding: 4px 8px; border-radius: 999px; font-size: 12px; font-weight: 700; display: inline-block; }
                .via-mvd { background: rgba(233,72,72,0.12); color: var(--danger); }
                .via-oc { background: rgba(31,157,85,0.12); color: var(--success); }
                table { width: 100%; border-collapse: collapse; margin-top: 10px; }
                th, td { padding: 10px; text-align: left; border-bottom: 1px solid rgba(0,0,0,0.06); font-size: 14px; vertical-align: top; }
                th { color: var(--muted); position: sticky; top: 0; background: var(--card); }
                .mini { padding: 6px 8px; font-size: 12px; margin: 2px 0; width: 100%; }
                .row-done { opacity: 0.45; }
                .table-wrapper { max-height: 600px; overflow: auto; }
                .brand-bar { display:flex; align-items:center; gap:10px; padding:10px 0 18px; }
                .brand-logo { width:46px; height:46px; border-radius:12px; background: linear-gradient(145deg, var(--accent), var(--accent-2)); display:flex; align-items:center; justify-content:center; color:#0f1b2f; font-weight:800; font-size:20px; }
                .brand-text { font-size:18px; font-weight:700; color: var(--text); letter-spacing: 0.5px; }
            </style>
        </head>
        <body>
            <div class="layout">
                <div class="grid">
                    <div class="card">
                        <div class="brand-bar">
                            <div class="brand-logo">S</div>
                            <div class="brand-text">SUCAN · Gestión de pedidos</div>
                        </div>
                        <h1>Pedidos de comidas</h1>
                        <p class="subtitle">Busca en el padrón, elige sucursal y guarda el pedido. La fecha se completa sola.</p>
                        <div class="row">
                            <div>
                                <label>Buscar producto</label>
                                <input id="search" placeholder="Código, nombre, marca..." autocomplete="off" />
                                <div id="results"></div>
                            </div>
                            <div>
                                <label>Sucursal que solicita</label>
                                <input id="sucursal" placeholder="Ej: PDE, MDO, etc." />
                                <label>Cantidad</label>
                                <input id="cantidad" type="number" min="1" value="1" />
                                <label>Observaciones</label>
                                <textarea id="obs" rows="3" placeholder="Notas adicionales"></textarea>
                                <div style="margin-top:8px;"><button id="btn-guardar" disabled>Guardar pedido</button></div>
                                <div id="seleccion-info" style="margin-top:10px; color: var(--muted);"></div>
                            </div>
                        </div>
                    </div>

                    <div class="card">
                        <h2 style="margin:0 0 10px;">Pedidos recientes</h2>
                        <div class="table-wrapper">
                            <table>
                                <thead>
                                    <tr>
                                        <th>Fecha</th><th>Sucursal</th><th>Producto</th><th>Marca</th><th>Vía</th><th>Cant</th><th>Estado</th><th>Obs</th><th>Acciones</th>
                                    </tr>
                                </thead>
                                <tbody id="tabla-pedidos"></tbody>
                            </table>
                        </div>
                    </div>
                </div>
            </div>
            <script>
                let selected = null;

                async function buscar(q) {
                    const res = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
                    if (!res.ok) return [];
                    return res.json();
                }

                async function listarPedidos() {
                    const res = await fetch('/api/orders');
                    if (!res.ok) return;
                    const data = await res.json();
                    const tbody = document.getElementById('tabla-pedidos');
                    tbody.innerHTML = '';
                    data.forEach(p => {
                        const tr = document.createElement('tr');
                        const viaClass = p.producto.via.includes('Montevideo') ? 'via-mvd' : 'via-oc';
                        const estados = {
                            pendiente: 'Pendiente',
                            llegado: 'Llegó',
                            avisado: 'Avisado cliente',
                            entregado: 'Entregado'
                        };
                        const estadoTxt = estados[p.estado] || p.estado;
                        const isDone = p.estado === 'entregado';
                        const showLlegado = p.estado === 'pendiente';
                        const showAvisado = p.estado === 'llegado';
                        const showFirmar = p.estado === 'avisado';
                        const showEntregar = p.estado === 'avisado' && p.firma;
                        let acciones = '';
                        if (showLlegado) acciones += `<button class="mini" data-action="llego" data-id="${p.id}">Llegó</button>`;
                        if (showAvisado) acciones += `<button class="mini" data-action="avisado" data-id="${p.id}">Avisar</button>`;
                        if (showFirmar) acciones += `<button class="mini" data-action="firma" data-id="${p.id}">Firmar</button>`;
                        if (showEntregar) acciones += `<button class="mini" data-action="entregado" data-id="${p.id}">Entregar</button>`;
                        tr.innerHTML = `
                            <td>${p.fecha_solicitud}</td>
                            <td>${p.sucursal}</td>
                            <td>${p.producto.nombre}</td>
                            <td>${p.producto.marca || '-'}</td>
                            <td><span class="tag ${viaClass}">${p.producto.via}</span></td>
                            <td>${p.cantidad}</td>
                            <td>${estadoTxt}
                                <div style="font-size:12px; color:var(--muted);">
                                    ${p.fecha_llegada ? 'Llegó: '+p.fecha_llegada+'<br>' : ''}
                                    ${p.fecha_aviso ? 'Aviso: '+p.fecha_aviso+'<br>' : ''}
                                    ${p.fecha_entrega ? 'Entrega: '+p.fecha_entrega+'<br>' : ''}
                                    ${p.firma ? 'Firma cargada' : ''}
                                </div>
                            </td>
                            <td>${p.observaciones || ''}</td>
                            <td>${acciones || '-'}</td>
                        `;
                        if (isDone) tr.classList.add('row-done');
                        tbody.appendChild(tr);
                    });
                    document.querySelectorAll('button.mini').forEach(btn => {
                        btn.onclick = async (e) => {
                            const id = e.target.getAttribute('data-id');
                            const action = e.target.getAttribute('data-action');
                            if (action === 'firma') {
                                window.open(`/firmar/${id}`, 'firma', 'width=480,height=560');
                                return;
                            }
                            await fetch(`/api/orders/${id}/estado`, {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ accion: action })
                            });
                            listarPedidos();
                        };
                    });
                }

                function renderResultados(items) {
                    const cont = document.getElementById('results');
                    cont.innerHTML = '';
                    items.forEach(item => {
                        const div = document.createElement('div');
                        const viaClass = item.via.includes('Montevideo') ? 'via-mvd' : 'via-oc';
                        div.className = 'result-item';
                        div.innerHTML = `
                            <div><strong>${item.nombre}</strong></div>
                            <div style="color:var(--muted); font-size:13px;">Código: ${item.codigo} | Marca: ${item.marca || '-'}</div>
                            <div style="margin-top:4px;"><span class="tag ${viaClass}">${item.via}</span></div>
                        `;
                        div.onclick = () => {
                            selected = item;
                            document.getElementById('seleccion-info').innerText = `Seleccionado: ${item.nombre} (${item.codigo}) - ${item.via}`;
                            document.getElementById('btn-guardar').disabled = false;
                        };
                        cont.appendChild(div);
                    });
                }

                let timer = null;
                document.getElementById('search').addEventListener('input', (e) => {
                    const q = e.target.value;
                    clearTimeout(timer);
                    timer = setTimeout(async () => {
                        const res = await buscar(q);
                        renderResultados(res);
                    }, 300);
                });

                document.getElementById('btn-guardar').addEventListener('click', async () => {
                    if (!selected) return;
                    const sucursal = document.getElementById('sucursal').value.trim();
                    const cantidad = parseInt(document.getElementById('cantidad').value, 10) || 1;
                    const obs = document.getElementById('obs').value.trim();
                    const payload = {
                        codigo: selected.codigo,
                        sucursal,
                        cantidad,
                        observaciones: obs
                    };
                    const res = await fetch('/api/orders', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload)
                    });
                    if (res.ok) {
                        document.getElementById('btn-guardar').disabled = true;
                        document.getElementById('seleccion-info').innerText = 'Pedido guardado.';
                        listarPedidos();
                    } else {
                        const err = await res.json().catch(() => ({}));
                        alert(err.error || 'Error al guardar');
                    }
                });

                listarPedidos();
                setInterval(listarPedidos, 5000);
            </script>
        </body>
        </html>
        """
    )


@app.route("/api/search")
def api_search():
    q = request.args.get("q", "")
    results = filter_products(q, limit=25)
    return jsonify(results)


@app.route("/api/orders", methods=["GET"])
def api_orders():
    return jsonify(load_orders())


@app.route("/api/orders", methods=["POST"])
def api_create_order():
    data = request.get_json(force=True)
    try:
        order = create_order(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(order)


@app.route("/api/orders/<order_id>/estado", methods=["POST"])
def api_update_order(order_id):
    data = request.get_json(force=True)
    action = data.get("accion")
    try:
        order = update_order_status(order_id, action)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(order)


@app.route("/api/orders/<order_id>/firma", methods=["POST"])
def api_save_signature(order_id):
    data = request.get_json(force=True)
    firma = data.get("firma")
    if not firma:
        return jsonify({"error": "Firma requerida"}), 400
    try:
        order = save_signature(order_id, firma)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(order)


@app.route("/firmar/<order_id>")
def firmar(order_id):
    return render_template("firma.html", order_id=order_id)


if __name__ == "__main__":
    import os

    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
