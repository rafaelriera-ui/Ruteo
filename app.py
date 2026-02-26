import streamlit as st
import pandas as pd
import requests
import folium
from streamlit_folium import st_folium
from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp
from haversine import haversine, Unit
import io
import datetime
import re
import time 

# --- CONFIGURACIÓN DE LA PÁGINA ---
st.set_page_config(page_title="Gestor de Rutas Logísticas", layout="wide")
st.title("🚚 Gestor de Rutas y Capas")

# --- API KEY PREDETERMINADA Y PERSONALIZADA ---
api_key_default = "5b3ce3597851110001cf62480080f4189d6143db946e7c7267b9343d"

st.sidebar.header("🔑 Conexión OpenRouteService")
api_key_user = st.sidebar.text_input("API Key propia (Solo si te quedas sin saldo diario)", type="password", help="Si te sale el error 'Quota exceeded', pega aquí tu propia API Key.")
api_key = api_key_user if api_key_user else api_key_default

if 'calculo_terminado' not in st.session_state:
    st.session_state['calculo_terminado'] = False

# --- BARRA LATERAL: CARGA ---
st.sidebar.markdown("---")
st.sidebar.header("Carga de Datos")
archivo_subido = st.sidebar.file_uploader("Sube tu archivo Excel", type=["xlsx", "xls"])

if archivo_subido is None:
    st.info("👈 Por favor, sube tu archivo Excel en la barra lateral para comenzar.")
    st.stop()

# --- FUNCIONES AUXILIARES ---
def preparar_coordenadas(coord_str):
    try:
        partes = str(coord_str).split(',')
        if len(partes) >= 2:
            lat = float(partes[0].strip())
            lon = float(partes[1].strip())
            return [lon, lat]
        return None
    except Exception:
        return None

def limpiar_nombre_excel(nombre):
    return re.sub(r'[\\/*?:\[\]]', '_', str(nombre))[:31]

def dibujar_geozona_circular(coordenadas_lon_lat, nombre_capa, color, mapa, mostrar_por_defecto=True):
    coords_lat_lon = [(p[1], p[0]) for p in coordenadas_lon_lat]
    if len(coords_lat_lon) > 0:
        avg_lat = sum([p[0] for p in coords_lat_lon]) / len(coords_lat_lon)
        avg_lon = sum([p[1] for p in coords_lat_lon]) / len(coords_lat_lon)
        centro = (avg_lat, avg_lon)

        max_radio_metros = 0
        for punto in coords_lat_lon:
            dist = haversine(centro, punto, unit=Unit.METERS)
            if dist > max_radio_metros:
                max_radio_metros = dist
        
        radio_final = max_radio_metros * 1.05 if max_radio_metros > 10 else 200

        capa = folium.FeatureGroup(name=nombre_capa, show=mostrar_por_defecto)
        folium.Circle(
            location=centro, radius=radio_final, color=color,
            fill=True, fill_color=color, fill_opacity=0.15, weight=2
        ).add_to(capa)
        capa.add_to(mapa)

# --- CONEXIÓN PURA BLINDADA CONTRA CAÍDAS DE SERVIDOR ---
def pedir_matriz_ors_con_reintento(body, headers):
    for intento in range(5): 
        try:
            resp = requests.post('https://api.openrouteservice.org/v2/matrix/driving-car', json=body, headers=headers, timeout=70)
            if resp.status_code == 200:
                return resp.json(), None
            elif "Quota" in resp.text or resp.status_code == 403:
                return None, "QUOTA_EXCEEDED"
            elif resp.status_code == 429 or "Rate limit" in resp.text:
                time.sleep(60) 
            elif resp.status_code >= 500 or "unknown" in resp.text.lower():
                time.sleep(5)
            else:
                return None, resp.text
        except requests.exceptions.RequestException:
            time.sleep(5)
    return None, "Superado el límite de reintentos. El servidor de mapas mundial está caído temporalmente."

def pedir_trazado_ors_con_reintento(body, headers):
    for intento in range(5):
        try:
            resp = requests.post('https://api.openrouteservice.org/v2/directions/driving-car/geojson', json=body, headers=headers, timeout=70)
            if resp.status_code == 200:
                return resp.json(), None
            elif "Quota" in resp.text or resp.status_code == 403:
                return None, "QUOTA_EXCEEDED"
            elif resp.status_code == 429 or "Rate limit" in resp.text:
                time.sleep(60)
            elif resp.status_code >= 500 or "unknown" in resp.text.lower():
                time.sleep(5)
            else:
                return None, resp.text
        except requests.exceptions.RequestException:
            time.sleep(5)
    return None, "Superado el límite de reintentos."

def obtener_matriz_masiva(lista_coords, headers):
    N = len(lista_coords)
    if N <= 50:
        body_matrix = {"locations": lista_coords, "metrics": ["distance", "duration"]}
        data, err = pedir_matriz_ors_con_reintento(body_matrix, headers)
        if data:
            return data['distances'], data['durations'], None
        return None, None, err
    else:
        matriz_dist = [[0.0] * N for _ in range(N)]
        matriz_dur = [[0.0] * N for _ in range(N)]
        chunk_size = 15 
        for i in range(0, N, chunk_size):
            for j in range(0, N, chunk_size):
                src_chunk = lista_coords[i : i+chunk_size]
                dst_chunk = lista_coords[j : j+chunk_size]
                locs = []
                src_indices, dst_indices = [], []
                for pt in src_chunk:
                    if pt not in locs: locs.append(pt)
                    src_indices.append(locs.index(pt))
                for pt in dst_chunk:
                    if pt not in locs: locs.append(pt)
                    dst_indices.append(locs.index(pt))
                body_matrix = {"locations": locs, "sources": src_indices, "destinations": dst_indices, "metrics": ["distance", "duration"]}
                data, err = pedir_matriz_ors_con_reintento(body_matrix, headers)
                if data:
                    dists, durs = data['distances'], data['durations']
                    for u in range(len(src_chunk)):
                        for v in range(len(dst_chunk)):
                            matriz_dist[i+u][j+v] = dists[u][v]
                            matriz_dur[i+u][j+v] = durs[u][v]
                else:
                    return None, None, err
                time.sleep(1.2) 
        return matriz_dist, matriz_dur, None

def obtener_trazado_masivo(coords_ordenadas, headers):
    total_dist, total_dur = 0, 0
    all_segments, merged_coordinates = [], []
    chunk_size = 40
    for i in range(0, len(coords_ordenadas) - 1, chunk_size - 1):
        chunk = coords_ordenadas[i:i + chunk_size]
        if len(chunk) < 2: break
        body_dirs = {"coordinates": chunk, "radiuses": [-1] * len(chunk)}
        data, err = pedir_trazado_ors_con_reintento(body_dirs, headers)
        if data:
            props = data['features'][0]['properties']['summary']
            segs = data['features'][0]['properties'].get('segments', [])
            geom = data['features'][0]['geometry']['coordinates']
            total_dist += props['distance']
            total_dur += props['duration']
            all_segments.extend(segs)
            if not merged_coordinates:
                merged_coordinates.extend(geom)
            else:
                merged_coordinates.extend(geom[1:])
        else:
            return None, err
        time.sleep(1.5)
    fake_geojson = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {"summary": {"distance": total_dist, "duration": total_dur}, "segments": all_segments},
            "geometry": {"type": "LineString", "coordinates": merged_coordinates}
        }]
    }
    return fake_geojson, None

# --- PROCESAMIENTO INICIAL DEL DATAFRAME ---
df = pd.read_excel(archivo_subido)
df.columns = df.columns.str.strip() 

for col in ['Coordenadas', 'Día']:
    if col not in df.columns:
        st.error(f"❌ No se encontró la columna '{col}' en tu archivo Excel.")
        st.stop()

if 'Ruta' not in df.columns:
    df['Ruta'] = "Sin Asignar"
df['Ruta'] = df['Ruta'].fillna("Sin Asignar")

df['Coords_Procesadas'] = df['Coordenadas'].apply(preparar_coordenadas)
df = df.dropna(subset=['Coords_Procesadas']).copy()

if df.empty:
    st.error("❌ No se encontraron coordenadas válidas en el archivo.")
    st.stop()

# --- BARRA LATERAL: FILTROS DE DÍA ---
st.sidebar.header("1. Filtro de Días")
dias_disponibles = df['Día'].unique().tolist()
todos_dias = st.sidebar.checkbox("✔️ Todos los Días", value=True)
if todos_dias:
    dias_seleccionados = dias_disponibles
else:
    dias_seleccionados = st.sidebar.multiselect("Días:", dias_disponibles, default=[])

if not dias_seleccionados:
    st.sidebar.warning("Selecciona al menos un Día.")
    st.stop()

df_filtrado_dias = df[df['Día'].isin(dias_seleccionados)]

# --- BARRA LATERAL: CONFIGURACIÓN DE RUTEO ---
st.sidebar.markdown("---")
st.sidebar.header("2. Estrategia de Ruteo")

# LAS 8 OPCIONES DEFINITIVAS
tipo_ruteo = st.sidebar.radio(
    "Selecciona cómo armar las rutas:",
    [
        "Ruteo según Excel (Orden Original)", 
        "Ruteo Optimizado (IA)", 
        "Creación de rutas propias (Ideal Libre)",
        "Creación de rutas propias (Departamental Flexible)",
        "Creación de rutas propias (Departamental Fijo)",
        "Creación de rutas propias (Ideal Libre - Patrón Fijo)",
        "Creación de rutas propias (Departamental Flexible - Patrón Fijo)",
        "Creación de rutas propias (Departamental Fijo - Patrón Fijo)"
    ]
)

opciones_inicio_dict = {}
opciones_fin_dict = {}
rutas_seleccionadas = []

if tipo_ruteo in ["Ruteo según Excel (Orden Original)", "Ruteo Optimizado (IA)"]:
    st.sidebar.markdown("**Filtro de Rutas**")
    rutas_disponibles = df_filtrado_dias['Ruta'].unique().tolist()
    todas_rutas = st.sidebar.checkbox("✔️ Todas las Rutas", value=True)
    if todas_rutas:
        rutas_seleccionadas = rutas_disponibles
    else:
        rutas_seleccionadas = st.sidebar.multiselect("Rutas:", rutas_disponibles, default=[])

    if not rutas_seleccionadas:
        st.sidebar.warning("Selecciona al menos una ruta.")
        st.stop()
        
    if tipo_ruteo == "Ruteo Optimizado (IA)":
        st.sidebar.markdown("---")
        st.sidebar.markdown("**📍 Configuración de Inicio y Fin**")
        st.sidebar.info("Elige dónde empezar y terminar. Si seleccionas 'IA Decide', el sistema buscará la opción más rápida.")
        for dia in dias_seleccionados:
            for ruta in rutas_seleccionadas:
                df_unicaruta = df_filtrado_dias[(df_filtrado_dias['Día'] == dia) & (df_filtrado_dias['Ruta'] == ruta)].reset_index(drop=True)
                if not df_unicaruta.empty:
                        lugares_lista = df_unicaruta['Lugar'].tolist()
                        id_ruta = f"{dia} - {ruta}"
                        st.sidebar.markdown(f"**Ruta:** {id_ruta}")
                        opciones_lugar = ["🤖 IA Decide"] + lugares_lista
                        sel_ini = st.sidebar.selectbox("Punto de Inicio:", opciones_lugar, index=0, key=f"ini_{id_ruta}")
                        sel_fin = st.sidebar.selectbox("Punto Final:", opciones_lugar, index=len(opciones_lugar)-1, key=f"fin_{id_ruta}")
                        opciones_inicio_dict[id_ruta] = sel_ini
                        opciones_fin_dict[id_ruta] = sel_fin
                        st.sidebar.markdown("<br>", unsafe_allow_html=True)

elif "Creación de rutas propias" in tipo_ruteo:
    st.sidebar.markdown("---")
    st.sidebar.header("Configuración de Flota Automática")
    
    if "Patrón Fijo" in tipo_ruteo:
        st.sidebar.info("🗓️ Modo Patrón Maestro: Analiza todos los días para crear un 'Molde Base' equilibrado que aplicará siempre igual.")
    elif "Fijo" in tipo_ruteo:
        st.sidebar.info("🏢 Modo Fijo: Corta el mapa y calcula flota 100% independiente por departamento. NUNCA mezcla zonas en un auto.")
    elif "Flexible" in tipo_ruteo:
        st.sidebar.info("🏘️ Modo Flexible: Agrupa por zona para dar orden, pero SÍ PERMITE cruzar fronteras si eso ahorra crear un vehículo entero.")
    else:
        st.sidebar.info("🚀 Modo Ideal Libre: Ignora fronteras geográficas. Prioriza únicamente tiempo, kilómetros y ahorro máximo de vehículos.")
        
    opciones_lugar_vrp = df_filtrado_dias['Lugar'].unique().tolist() if dias_seleccionados else []
    punto_final_vrp = st.sidebar.selectbox("📍 Punto final de TODAS las rutas:", opciones_lugar_vrp)
    
    col_salida, col_llegada = st.sidebar.columns(2)
    with col_salida:
        hora_salida_vrp = st.time_input("Hora Salida", datetime.time(8, 0))
    with col_llegada:
        hora_llegada_vrp = st.time_input("Límite Llegada", datetime.time(14, 30))
        
    min_parada_vrp = st.sidebar.number_input("Minutos espera por parada", min_value=0, value=15, step=1)
    
    start_dt = datetime.datetime.combine(datetime.date.today(), hora_salida_vrp)
    end_dt = datetime.datetime.combine(datetime.date.today(), hora_llegada_vrp)
    max_time_sec = int((end_dt - start_dt).total_seconds())
    
    if max_time_sec <= 0:
        st.sidebar.error("❌ El horario de llegada debe ser mayor al de salida.")
        st.stop()

# --- BOTÓN DE CÁLCULO ---
if st.sidebar.button("🗺️ Calcular Rutas", type="primary"):
    with st.spinner("Nivelando cargas y minimizando la flota vehicular requerida..."):
        lat_centro = df_filtrado_dias.iloc[0]['Coords_Procesadas'][1]
        lon_centro = df_filtrado_dias.iloc[0]['Coords_Procesadas'][0]
        mapa_calculado = folium.Map(location=[lat_centro, lon_centro], zoom_start=11)
        
        colores = ['blue', 'red', 'green', 'purple', 'orange', 'darkred', 'cadetblue', 'darkblue', 'pink', 'lightgreen']
        datos_para_resumen = []
        color_idx = 0
        headers = {'Authorization': api_key, 'Content-Type': 'application/json'}

        # ==========================================================
        # LÓGICA 1 Y 2: RUTEO CLÁSICO Y OPTIMIZADO
        # ==========================================================
        if tipo_ruteo in ["Ruteo según Excel (Orden Original)", "Ruteo Optimizado (IA)"]:
            for dia in dias_seleccionados:
                df_dia_general = df[df['Día'] == dia]
                if not df_dia_general.empty:
                    dibujar_geozona_circular(df_dia_general['Coords_Procesadas'].tolist(), f"🌍 DÍA: {dia}", "black", mapa_calculado)

                for ruta in rutas_seleccionadas:
                    df_ruta = df[(df['Día'] == dia) & (df['Ruta'] == ruta)].copy().reset_index(drop=True)
                    if df_ruta.empty: continue
                    
                    id_unico = f"{dia} - {ruta}"
                    color_actual = colores[color_idx % len(colores)]
                    color_idx += 1
                    
                    lista_coords = df_ruta['Coords_Procesadas'].tolist()
                    dibujar_geozona_circular(lista_coords, f"🗺️ {ruta}", color_actual, mapa_calculado)
                    
                    nodos_ordenados = []
                    coords_ordenadas = []

                    if tipo_ruteo == "Ruteo según Excel (Orden Original)":
                        nodos_ordenados = list(range(len(df_ruta)))
                        coords_ordenadas = lista_coords
                    else: 
                        num_locs = len(lista_coords)
                        if num_locs < 2:
                            nodos_ordenados = [0]
                            coords_ordenadas = lista_coords
                        else:
                            matriz_dist, matriz_dur, err_matriz = obtener_matriz_masiva(lista_coords, headers)
                            if err_matriz == "QUOTA_EXCEEDED":
                                st.error(f"❌ ¡SALDO DIARIO AGOTADO en la ruta {ruta}! Pega tu propia clave en el menú lateral.")
                                st.stop()
                                
                            if not err_matriz:
                                N = num_locs
                                extended_dist = [[0] * (N + 2) for _ in range(N + 2)]
                                
                                for i in range(N):
                                    for j in range(N):
                                        val = matriz_dist[i][j]
                                        extended_dist[i][j] = int(val) if val is not None else 99999999
                                        
                                sel_inicio = opciones_inicio_dict.get(id_unico, "🤖 IA Decide")
                                if sel_inicio == "🤖 IA Decide":
                                    for j in range(N): extended_dist[N][j] = 0
                                else:
                                    idx_inicio = df_ruta['Lugar'].tolist().index(sel_inicio)
                                    for j in range(N): extended_dist[N][j] = 99999999
                                    extended_dist[N][idx_inicio] = 0
                                    
                                sel_fin = opciones_fin_dict.get(id_unico, "🤖 IA Decide")
                                if sel_fin == "🤖 IA Decide":
                                    for i in range(N): extended_dist[i][N+1] = 0
                                else:
                                    idx_fin = df_ruta['Lugar'].tolist().index(sel_fin)
                                    for i in range(N): extended_dist[i][N+1] = 99999999
                                    extended_dist[idx_fin][N+1] = 0

                                manager = pywrapcp.RoutingIndexManager(N + 2, 1, [N], [N+1])
                                routing = pywrapcp.RoutingModel(manager)
                                
                                def distance_callback(from_index, to_index):
                                    from_node = manager.IndexToNode(from_index)
                                    to_node = manager.IndexToNode(to_index)
                                    return extended_dist[from_node][to_node]
                                    
                                transit_callback_index = routing.RegisterTransitCallback(distance_callback)
                                routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)
                                
                                search_parameters = pywrapcp.DefaultRoutingSearchParameters()
                                search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.SAVINGS
                                search_parameters.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
                                search_parameters.time_limit.seconds = 5 
                                
                                solution = routing.SolveWithParameters(search_parameters)
                                
                                if solution:
                                    idx = routing.Start(0)
                                    while not routing.IsEnd(idx):
                                        node = manager.IndexToNode(idx)
                                        if node < N:
                                            nodos_ordenados.append(node)
                                        idx = solution.Value(routing.NextVar(idx))
                                    coords_ordenadas = [lista_coords[i] for i in nodos_ordenados]
                                else:
                                    st.error(f"No se encontró solución lógica para {ruta}.")
                                    continue
                            else:
                                st.error(f"Error Matriz en {ruta}: {err_matriz}")
                                continue

                    if len(coords_ordenadas) > 1:
                        geojson, err_dirs = obtener_trazado_masivo(coords_ordenadas, headers)
                        if err_dirs == "QUOTA_EXCEEDED":
                            st.error(f"❌ ¡SALDO DIARIO AGOTADO al dibujar {ruta}!")
                            st.stop()
                            
                        if not err_dirs:
                            props = geojson['features'][0]['properties']['summary']
                            segments = geojson['features'][0]['properties'].get('segments', [])
                            
                            paradas_info = []
                            for nodo_idx in nodos_ordenados:
                                fila = df_ruta.iloc[nodo_idx]
                                paradas_info.append({
                                    "Día": fila.get('Día',''), "Ruta": fila.get('Ruta',''),
                                    "Departamento": fila.get('Departamento',''), "Lugar": fila.get('Lugar',''),
                                    "Coordenadas": fila.get('Coordenadas','')
                                })

                            datos_para_resumen.append({
                                "id_unico": id_unico, "dia": dia, "ruta": ruta,
                                "puntos": len(df_ruta), "dist_km": round(props['distance']/1000, 2),
                                "drive_mins": round(props['duration']/60, 0), "color": color_actual,
                                "paradas": paradas_info, "segmentos": segments
                            })
                            
                            fg_trazado = folium.FeatureGroup(name=f"🛣️ Trazado: {ruta}")
                            folium.GeoJson(geojson, style_function=lambda x, c=color_actual: {'color':c, 'weight':4, 'opacity':0.8}).add_to(fg_trazado)
                            
                            for i, nodo_idx in enumerate(nodos_ordenados):
                                fila = df_ruta.iloc[nodo_idx]
                                lat, lon = fila['Coords_Procesadas'][1], fila['Coords_Procesadas'][0]
                                popup_txt = f"<b>{i+1}. {fila.get('Lugar','')}</b><br>{fila.get('Departamento','')}"
                                icon_html = f"<div style='background:{color_actual};color:white;border-radius:50%;width:20px;text-align:center;border:1px solid white;font-weight:bold;font-size:10pt'>{i+1}</div>"
                                folium.Marker([lat, lon], popup=popup_txt, icon=folium.DivIcon(html=icon_html)).add_to(fg_trazado)
                            fg_trazado.add_to(mapa_calculado)
                        else:
                            st.error(f"Error trazando calles de {ruta}: {err_dirs}")

        # ==========================================================
        # LÓGICA 3 Y 4: CREACIÓN DE RUTAS PROPIAS (LIBRE Y FLEXIBLE DIARIO)
        # ==========================================================
        elif tipo_ruteo in ["Creación de rutas propias (Ideal Libre)", "Creación de rutas propias (Departamental Flexible)"]:
            destino_row = df_filtrado_dias[df_filtrado_dias['Lugar'] == punto_final_vrp].iloc[0]
            
            for dia in dias_seleccionados:
                df_dia = df[df['Día'] == dia].copy().reset_index(drop=True)
                if punto_final_vrp not in df_dia['Lugar'].values:
                    df_dia = pd.concat([df_dia, destino_row.to_frame().T], ignore_index=True)
                
                end_idx = df_dia[df_dia['Lugar'] == punto_final_vrp].index[0]
                lista_coords = df_dia['Coords_Procesadas'].tolist()
                num_locs = len(lista_coords)
                
                if num_locs < 2: continue
                dibujar_geozona_circular(lista_coords, f"🌍 DÍA: {dia} (Zona)", "black", mapa_calculado)

                matriz_dist, matriz_dur, err_matriz = obtener_matriz_masiva(lista_coords, headers)
                if err_matriz == "QUOTA_EXCEEDED":
                    st.error(f"❌ ¡SALDO DIARIO AGOTADO en el Día {dia}!")
                    st.stop()
                    
                if not err_matriz:
                    dummy_idx = num_locs
                    for i in range(num_locs):
                        matriz_dist[i].append(0); matriz_dur[i].append(0)
                    matriz_dist.append([0] * (num_locs + 1)); matriz_dur.append([0] * (num_locs + 1))
                    
                    for i in range(num_locs):
                        if i != dummy_idx and i != end_idx:
                            val_dur = matriz_dur[i][end_idx]
                            if val_dur is None:
                                st.error(f"❌ Error de Mapa: El punto '{df_dia.iloc[i]['Lugar']}' no tiene conexión por calle.")
                                st.stop()
                            tiempo_minimo_viaje = int(min_parada_vrp * 60) + int(val_dur)
                            if tiempo_minimo_viaje > max_time_sec:
                                st.error(f"❌ Error Físico Real: Ir desde '{df_dia.iloc[i]['Lugar']}' hasta el destino final toma por sí solo {tiempo_minimo_viaje//60} min.")
                                st.stop()

                    num_vehicles = num_locs 
                    manager = pywrapcp.RoutingIndexManager(num_locs + 1, num_vehicles, [dummy_idx] * num_vehicles, [int(end_idx)] * num_vehicles)
                    routing = pywrapcp.RoutingModel(manager)
                    
                    def distance_callback(from_index, to_index):
                        from_node = manager.IndexToNode(from_index)
                        to_node = manager.IndexToNode(to_index)
                        val = matriz_dist[from_node][to_node]
                        dist = int(val) if val is not None else 99999999 
                        
                        # Penalidad Departamental Flexible 
                        if "Flexible" in tipo_ruteo:
                            if from_node < num_locs and to_node < num_locs and from_node != end_idx and to_node != end_idx and from_node != dummy_idx:
                                dept_f = str(df_dia.iloc[from_node].get('Departamento', '')).strip().lower()
                                dept_t = str(df_dia.iloc[to_node].get('Departamento', '')).strip().lower()
                                if dept_f and dept_t and dept_f != dept_t:
                                    dist += 50000 
                        return dist
                        
                    transit_callback_index = routing.RegisterTransitCallback(distance_callback)
                    routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)
                    
                    # 🔴 BLOQUEO TOTAL DE AUTOS FANTASMAS: Obliga a usar la cantidad mínima de flota
                    routing.SetFixedCostOfAllVehicles(5000000)
                    
                    def time_callback(from_index, to_index):
                        from_node = manager.IndexToNode(from_index)
                        to_node = manager.IndexToNode(to_index)
                        val_dur = matriz_dur[from_node][to_node]
                        drive_time = int(val_dur) if val_dur is not None else 99999999 
                        wait_time = int(min_parada_vrp * 60) if to_node != dummy_idx and to_node != end_idx else 0
                        return drive_time + wait_time
                        
                    time_callback_index = routing.RegisterTransitCallback(time_callback)
                    routing.AddDimension(time_callback_index, 0, max_time_sec, True, "Time")
                    
                    # 🟢 EQUILIBRADOR SUAVE: Nivelará el tiempo solo entre los pocos autos que se abran
                    time_dimension = routing.GetDimensionOrDie("Time")
                    time_dimension.SetGlobalSpanCostCoefficient(100)

                    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
                    search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.SAVINGS
                    search_parameters.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
                    search_parameters.time_limit.seconds = 20 
                    
                    solution = routing.SolveWithParameters(search_parameters)
                    
                    if solution:
                        vehiculo_real_count = 1
                        for vehicle_id in range(num_vehicles):
                            index = routing.Start(vehicle_id)
                            first_visit = solution.Value(routing.NextVar(index))
                            if manager.IndexToNode(first_visit) == end_idx: continue 
                            nodos_ordenados = []
                            while not routing.IsEnd(index):
                                node = manager.IndexToNode(index)
                                if node != dummy_idx:
                                    nodos_ordenados.append(node)
                                index = solution.Value(routing.NextVar(index))
                            nodos_ordenados.append(end_idx)
                            
                            coords_ordenadas = [lista_coords[i] for i in nodos_ordenados]
                            ruta_nombre = f"Auto {vehiculo_real_count}"
                            id_unico = f"{dia} - {ruta_nombre}"
                            vehiculo_real_count += 1
                            color_actual = colores[color_idx % len(colores)]
                            color_idx += 1
                            
                            geojson, err_dirs = obtener_trazado_masivo(coords_ordenadas, headers)
                            if err_dirs == "QUOTA_EXCEEDED":
                                st.error(f"❌ ¡SALDO DIARIO AGOTADO al dibujar {ruta_nombre}!")
                                st.stop()
                                
                            if not err_dirs:
                                props = geojson['features'][0]['properties']['summary']
                                segments = geojson['features'][0]['properties'].get('segments', [])
                                paradas_info = []
                                for nodo_idx in nodos_ordenados:
                                    fila = df_dia.iloc[nodo_idx]
                                    paradas_info.append({
                                        "Día": fila.get('Día',''), "Ruta": ruta_nombre, 
                                        "Departamento": fila.get('Departamento',''), "Lugar": fila.get('Lugar',''),
                                        "Coordenadas": fila.get('Coordenadas','')
                                    })
                                datos_para_resumen.append({
                                    "id_unico": id_unico, "dia": dia, "ruta": ruta_nombre,
                                    "puntos": len(nodos_ordenados), "dist_km": round(props['distance']/1000, 2),
                                    "drive_mins": round(props['duration']/60, 0), "color": color_actual,
                                    "paradas": paradas_info, "segmentos": segments
                                })
                                fg_trazado = folium.FeatureGroup(name=f"🛣️ Trazado: {ruta_nombre} ({dia})")
                                folium.GeoJson(geojson, style_function=lambda x, c=color_actual: {'color':c, 'weight':4, 'opacity':0.8}).add_to(fg_trazado)
                                for i, nodo_idx in enumerate(nodos_ordenados):
                                    fila = df_dia.iloc[nodo_idx]
                                    lat, lon = fila['Coords_Procesadas'][1], fila['Coords_Procesadas'][0]
                                    popup_txt = f"<b>{i+1}. {fila.get('Lugar','')}</b><br>{fila.get('Departamento','')}"
                                    icon_html = f"<div style='background:{color_actual};color:white;border-radius:50%;width:20px;text-align:center;border:1px solid white;font-weight:bold;font-size:10pt'>{i+1}</div>"
                                    folium.Marker([lat, lon], popup=popup_txt, icon=folium.DivIcon(html=icon_html)).add_to(fg_trazado)
                                fg_trazado.add_to(mapa_calculado)
                            else:
                                st.error(f"Error en trazado: {err_dirs}")
                    else:
                        st.error(f"❌ Imposible matemático en el {dia}.")
                else:
                    st.error(f"Error Matriz {dia}: {err_matriz}")

        # ==========================================================
        # LÓGICA 5: CREACIÓN DE RUTAS PROPIAS (DEPARTAMENTAL FIJO - NORMAL)
        # ==========================================================
        elif tipo_ruteo == "Creación de rutas propias (Departamental Fijo)":
            destino_row = df_filtrado_dias[df_filtrado_dias['Lugar'] == punto_final_vrp].iloc[0]
            
            for dia in dias_seleccionados:
                df_dia_completo = df[df['Día'] == dia].copy().reset_index(drop=True)
                lista_coords_dia = df_dia_completo['Coords_Procesadas'].tolist()
                if len(lista_coords_dia) > 1:
                    dibujar_geozona_circular(lista_coords_dia, f"🌍 DÍA: {dia} (Zona Global)", "black", mapa_calculado)

                dept_series = df_dia_completo[df_dia_completo['Lugar'] != punto_final_vrp]['Departamento']
                departamentos = [d for d in dept_series.unique() if pd.notna(d) and str(d).strip() != '']
                vehiculo_real_count = 1
                
                for dept in departamentos:
                    df_dept = df_dia_completo[(df_dia_completo['Departamento'] == dept) & (df_dia_completo['Lugar'] != punto_final_vrp)].copy().reset_index(drop=True)
                    if df_dept.empty: continue
                    
                    df_dept = pd.concat([df_dept, destino_row.to_frame().T], ignore_index=True)
                    end_idx = len(df_dept) - 1
                    lista_coords = df_dept['Coords_Procesadas'].tolist()
                    num_locs = len(lista_coords)
                    
                    if num_locs < 2: continue

                    matriz_dist, matriz_dur, err_matriz = obtener_matriz_masiva(lista_coords, headers)
                    if err_matriz == "QUOTA_EXCEEDED":
                        st.error(f"❌ ¡SALDO DIARIO AGOTADO en el Día {dia}, Depto {dept}!")
                        st.stop()
                        
                    if not err_matriz:
                        dummy_idx = num_locs
                        for i in range(num_locs):
                            matriz_dist[i].append(0); matriz_dur[i].append(0)
                        matriz_dist.append([0] * (num_locs + 1)); matriz_dur.append([0] * (num_locs + 1))
                        
                        for i in range(num_locs):
                            if i != dummy_idx and i != end_idx:
                                val_dur = matriz_dur[i][end_idx]
                                if val_dur is None:
                                    st.error(f"❌ Error de Mapa: El punto '{df_dept.iloc[i]['Lugar']}' no tiene conexión por calle.")
                                    st.stop()
                                tiempo_minimo_viaje = int(min_parada_vrp * 60) + int(val_dur)
                                if tiempo_minimo_viaje > max_time_sec:
                                    st.error(f"❌ Error Físico Real: Ir desde '{df_dept.iloc[i]['Lugar']}' ({dept}) hasta el destino toma {tiempo_minimo_viaje//60} min reales.")
                                    st.stop()

                        num_vehicles = num_locs 
                        manager = pywrapcp.RoutingIndexManager(num_locs + 1, num_vehicles, [dummy_idx] * num_vehicles, [int(end_idx)] * num_vehicles)
                        routing = pywrapcp.RoutingModel(manager)
                        
                        def distance_callback(from_index, to_index):
                            from_node = manager.IndexToNode(from_index)
                            to_node = manager.IndexToNode(to_index)
                            val = matriz_dist[from_node][to_node]
                            dist = int(val) if val is not None else 99999999 
                            return dist
                            
                        transit_callback_index = routing.RegisterTransitCallback(distance_callback)
                        routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)
                        
                        # BLOQUEO DE FLOTA Y EQUILIBRADOR
                        routing.SetFixedCostOfAllVehicles(5000000)
                        
                        def time_callback(from_index, to_index):
                            from_node = manager.IndexToNode(from_index)
                            to_node = manager.IndexToNode(to_index)
                            val_dur = matriz_dur[from_node][to_node]
                            drive_time = int(val_dur) if val_dur is not None else 99999999 
                            wait_time = int(min_parada_vrp * 60) if to_node != dummy_idx and to_node != end_idx else 0
                            return drive_time + wait_time
                            
                        time_callback_index = routing.RegisterTransitCallback(time_callback)
                        routing.AddDimension(time_callback_index, 0, max_time_sec, True, "Time")
                        
                        time_dimension = routing.GetDimensionOrDie("Time")
                        time_dimension.SetGlobalSpanCostCoefficient(100)

                        search_parameters = pywrapcp.DefaultRoutingSearchParameters()
                        search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.SAVINGS
                        search_parameters.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
                        search_parameters.time_limit.seconds = 10 
                        
                        solution = routing.SolveWithParameters(search_parameters)
                        
                        if solution:
                            for vehicle_id in range(num_vehicles):
                                index = routing.Start(vehicle_id)
                                first_visit = solution.Value(routing.NextVar(index))
                                if manager.IndexToNode(first_visit) == end_idx: continue 
                                nodos_ordenados = []
                                while not routing.IsEnd(index):
                                    node = manager.IndexToNode(index)
                                    if node != dummy_idx: nodos_ordenados.append(node)
                                    index = solution.Value(routing.NextVar(index))
                                nodos_ordenados.append(end_idx)
                                
                                coords_ordenadas = [lista_coords[i] for i in nodos_ordenados]
                                nombre_dept_limpio = str(dept).strip()
                                ruta_nombre = f"Auto {vehiculo_real_count} ({nombre_dept_limpio})"
                                id_unico = f"{dia} - {ruta_nombre}"
                                vehiculo_real_count += 1
                                color_actual = colores[color_idx % len(colores)]
                                color_idx += 1
                                
                                geojson, err_dirs = obtener_trazado_masivo(coords_ordenadas, headers)
                                if err_dirs == "QUOTA_EXCEEDED":
                                    st.error(f"❌ ¡SALDO DIARIO AGOTADO al dibujar {ruta_nombre}!")
                                    st.stop()
                                if not err_dirs:
                                    props = geojson['features'][0]['properties']['summary']
                                    segments = geojson['features'][0]['properties'].get('segments', [])
                                    paradas_info = []
                                    for nodo_idx in nodos_ordenados:
                                        fila = df_dept.iloc[nodo_idx]
                                        paradas_info.append({
                                            "Día": fila.get('Día',''), "Ruta": ruta_nombre, 
                                            "Departamento": fila.get('Departamento',''), "Lugar": fila.get('Lugar',''),
                                            "Coordenadas": fila.get('Coordenadas','')
                                        })
                                    datos_para_resumen.append({
                                        "id_unico": id_unico, "dia": dia, "ruta": ruta_nombre,
                                        "puntos": len(nodos_ordenados), "dist_km": round(props['distance']/1000, 2),
                                        "drive_mins": round(props['duration']/60, 0), "color": color_actual,
                                        "paradas": paradas_info, "segmentos": segments
                                    })
                                    fg_trazado = folium.FeatureGroup(name=f"🛣️ Trazado: {ruta_nombre} ({dia})")
                                    folium.GeoJson(geojson, style_function=lambda x, c=color_actual: {'color':c, 'weight':4, 'opacity':0.8}).add_to(fg_trazado)
                                    for i, nodo_idx in enumerate(nodos_ordenados):
                                        fila = df_dept.iloc[nodo_idx]
                                        lat, lon = fila['Coords_Procesadas'][1], fila['Coords_Procesadas'][0]
                                        popup_txt = f"<b>{i+1}. {fila.get('Lugar','')}</b><br>{fila.get('Departamento','')}"
                                        icon_html = f"<div style='background:{color_actual};color:white;border-radius:50%;width:20px;text-align:center;border:1px solid white;font-weight:bold;font-size:10pt'>{i+1}</div>"
                                        folium.Marker([lat, lon], popup=popup_txt, icon=folium.DivIcon(html=icon_html)).add_to(fg_trazado)
                                    fg_trazado.add_to(mapa_calculado)
                                else:
                                    st.error(f"Error en trazado: {err_dirs}")
                        else:
                            st.error(f"❌ Imposible matemático en el {dia} para {dept}.")
                    else:
                        st.error(f"Error Matriz {dia} - {dept}: {err_matriz}")

        # ==========================================================
        # LÓGICA 6, 7 Y 8: CREACIÓN DE RUTAS PROPIAS (PATRÓN MAESTRO)
        # ==========================================================
        elif "Patrón Fijo" in tipo_ruteo:
            destino_row = df_filtrado_dias[df_filtrado_dias['Lugar'] == punto_final_vrp].iloc[0]

            st.info("🧠 Generando Patrón Maestro Equilibrado y minimizando flota...")
            
            df_master_total = df_filtrado_dias[df_filtrado_dias['Lugar'] != punto_final_vrp].drop_duplicates(subset=['Lugar']).copy().reset_index(drop=True)

            rutas_maestras_base = []
            vehiculo_real_count = 1

            # A) MODO PATRÓN FIJO DEPARTAMENTAL FIJO (CORTA EL MAPA)
            if "Departamental Fijo" in tipo_ruteo:
                dept_series = df_master_total['Departamento']
                departamentos = [d for d in dept_series.unique() if pd.notna(d) and str(d).strip() != '']

                for dept in departamentos:
                    df_target = df_master_total[df_master_total['Departamento'] == dept].copy().reset_index(drop=True)
                    if df_target.empty: continue
                    df_target = pd.concat([df_target, destino_row.to_frame().T], ignore_index=True)

                    lista_coords = df_target['Coords_Procesadas'].tolist()
                    num_locs = len(lista_coords)
                    end_idx = num_locs - 1
                    if num_locs < 2: continue

                    matriz_dist, matriz_dur, err_matriz = obtener_matriz_masiva(lista_coords, headers)
                    if err_matriz: st.error(err_matriz); st.stop()

                    dummy_idx = num_locs
                    for i in range(num_locs):
                        matriz_dist[i].append(0); matriz_dur[i].append(0)
                    matriz_dist.append([0]*(num_locs+1)); matriz_dur.append([0]*(num_locs+1))

                    num_vehicles = num_locs
                    manager = pywrapcp.RoutingIndexManager(num_locs + 1, num_vehicles, [dummy_idx]*num_vehicles, [end_idx]*num_vehicles)
                    routing = pywrapcp.RoutingModel(manager)

                    def d_call(f, t):
                        fn, tn = manager.IndexToNode(f), manager.IndexToNode(t)
                        v = matriz_dist[fn][tn]
                        dist = int(v) if v is not None else 99999999
                        return dist

                    def t_call(f, t):
                        fn, tn = manager.IndexToNode(f), manager.IndexToNode(t)
                        vd = matriz_dur[fn][tn]
                        d_time = int(vd) if vd is not None else 99999999
                        wt = int(min_parada_vrp*60) if tn != dummy_idx and tn != end_idx else 0
                        return d_time + wt

                    transit_cb = routing.RegisterTransitCallback(d_call)
                    routing.SetArcCostEvaluatorOfAllVehicles(transit_cb)
                    
                    # BLOQUEO Y EQUILIBRIO
                    routing.SetFixedCostOfAllVehicles(5000000)
                    time_cb = routing.RegisterTransitCallback(t_call)
                    routing.AddDimension(time_cb, 0, max_time_sec, True, "Time")
                    time_dim = routing.GetDimensionOrDie("Time")
                    time_dim.SetGlobalSpanCostCoefficient(100)

                    search_params = pywrapcp.DefaultRoutingSearchParameters()
                    search_params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.SAVINGS
                    search_params.time_limit.seconds = 10

                    sol = routing.SolveWithParameters(search_params)
                    if sol:
                        for vid in range(num_vehicles):
                            idx = routing.Start(vid)
                            fv = sol.Value(routing.NextVar(idx))
                            if manager.IndexToNode(fv) == end_idx: continue

                            nodos_ord = []
                            while not routing.IsEnd(idx):
                                n = manager.IndexToNode(idx)
                                if n != dummy_idx: nodos_ord.append(n)
                                idx = sol.Value(routing.NextVar(idx))
                            nodos_ord.append(end_idx)

                            lugares = [df_target.iloc[i]['Lugar'] for i in nodos_ord]
                            r_name = f"Auto {vehiculo_real_count} ({str(dept).strip()})"
                            rutas_maestras_base.append({"nombre": r_name, "lugares": lugares, "color_idx": vehiculo_real_count-1})
                            vehiculo_real_count += 1

            # B) MODO PATRÓN (IDEAL LIBRE O DEPARTAMENTAL FLEXIBLE)
            else:
                df_target = pd.concat([df_master_total, destino_row.to_frame().T], ignore_index=True)
                lista_coords = df_target['Coords_Procesadas'].tolist()
                num_locs = len(lista_coords)
                end_idx = num_locs - 1

                if num_locs >= 2:
                    matriz_dist, matriz_dur, err_matriz = obtener_matriz_masiva(lista_coords, headers)
                    if err_matriz: st.error(err_matriz); st.stop()

                    dummy_idx = num_locs
                    for i in range(num_locs):
                        matriz_dist[i].append(0); matriz_dur[i].append(0)
                    matriz_dist.append([0]*(num_locs+1)); matriz_dur.append([0]*(num_locs+1))

                    num_vehicles = num_locs
                    manager = pywrapcp.RoutingIndexManager(num_locs + 1, num_vehicles, [dummy_idx]*num_vehicles, [end_idx]*num_vehicles)
                    routing = pywrapcp.RoutingModel(manager)

                    def d_call(f, t):
                        fn, tn = manager.IndexToNode(f), manager.IndexToNode(t)
                        v = matriz_dist[fn][tn]
                        dist = int(v) if v is not None else 99999999
                        
                        if "Departamental Flexible" in tipo_ruteo:
                            if fn < num_locs and tn < num_locs and fn != end_idx and tn != end_idx and fn != dummy_idx:
                                dept_f = str(df_target.iloc[fn].get('Departamento', '')).strip().lower()
                                dept_t = str(df_target.iloc[tn].get('Departamento', '')).strip().lower()
                                if dept_f and dept_t and dept_f != dept_t:
                                    dist += 50000
                        return dist

                    def t_call(f, t):
                        fn, tn = manager.IndexToNode(f), manager.IndexToNode(t)
                        vd = matriz_dur[fn][tn]
                        d_time = int(vd) if vd is not None else 99999999
                        wt = int(min_parada_vrp*60) if tn != dummy_idx and tn != end_idx else 0
                        return d_time + wt

                    transit_cb = routing.RegisterTransitCallback(d_call)
                    routing.SetArcCostEvaluatorOfAllVehicles(transit_cb)
                    
                    # BLOQUEO Y EQUILIBRIO GLOBAL
                    routing.SetFixedCostOfAllVehicles(5000000)
                    time_cb = routing.RegisterTransitCallback(t_call)
                    routing.AddDimension(time_cb, 0, max_time_sec, True, "Time")
                    time_dim = routing.GetDimensionOrDie("Time")
                    time_dim.SetGlobalSpanCostCoefficient(100)

                    search_params = pywrapcp.DefaultRoutingSearchParameters()
                    search_params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.SAVINGS
                    search_params.time_limit.seconds = 20

                    sol = routing.SolveWithParameters(search_params)
                    if sol:
                        for vid in range(num_vehicles):
                            idx = routing.Start(vid)
                            fv = sol.Value(routing.NextVar(idx))
                            if manager.IndexToNode(fv) == end_idx: continue

                            nodos_ord = []
                            while not routing.IsEnd(idx):
                                n = manager.IndexToNode(idx)
                                if n != dummy_idx: nodos_ord.append(n)
                                idx = sol.Value(routing.NextVar(idx))
                            nodos_ord.append(end_idx)

                            lugares = [df_target.iloc[i]['Lugar'] for i in nodos_ord]
                            r_name = f"Auto {vehiculo_real_count}"
                            rutas_maestras_base.append({"nombre": r_name, "lugares": lugares, "color_idx": vehiculo_real_count-1})
                            vehiculo_real_count += 1

            # --- 3. APLICAR EL PATRÓN A CADA DÍA ---
            st.info("🗓️ Imprimiendo el Patrón Maestro en los días seleccionados (saltando clientes sin pedido)...")
            for dia in dias_seleccionados:
                df_dia = df[df['Día'] == dia].copy().reset_index(drop=True)
                if punto_final_vrp not in df_dia['Lugar'].values:
                    df_dia = pd.concat([df_dia, destino_row.to_frame().T], ignore_index=True)

                lugares_del_dia = set(df_dia['Lugar'].tolist())
                lista_coords_dia = df_dia['Coords_Procesadas'].tolist()
                
                if len(lista_coords_dia) > 1:
                    dibujar_geozona_circular(lista_coords_dia, f"🌍 DÍA: {dia}", "black", mapa_calculado)

                for ruta_maestra in rutas_maestras_base:
                    lugares_hoy = [l for l in ruta_maestra['lugares'] if l in lugares_del_dia]

                    if len(lugares_hoy) < 2: continue 

                    if lugares_hoy[-1] != punto_final_vrp:
                        if punto_final_vrp in lugares_hoy:
                            lugares_hoy.remove(punto_final_vrp)
                        lugares_hoy.append(punto_final_vrp)

                    filas_hoy = []
                    coords_ordenadas = []
                    for l in lugares_hoy:
                        fila = df_dia[df_dia['Lugar'] == l].iloc[0]
                        filas_hoy.append(fila)
                        coords_ordenadas.append(fila['Coords_Procesadas'])

                    df_ruta_hoy = pd.DataFrame(filas_hoy)
                    ruta_nombre = ruta_maestra["nombre"]
                    id_unico = f"{dia} - {ruta_nombre}"
                    color_actual = colores[ruta_maestra["color_idx"] % len(colores)]

                    geojson, err_dirs = obtener_trazado_masivo(coords_ordenadas, headers)
                    if err_dirs:
                        st.error(err_dirs)
                        continue

                    props = geojson['features'][0]['properties']['summary']
                    segments = geojson['features'][0]['properties'].get('segments', [])

                    paradas_info = []
                    for _, row_hoy in df_ruta_hoy.iterrows():
                        paradas_info.append({
                            "Día": row_hoy.get('Día',''), "Ruta": ruta_nombre,
                            "Departamento": row_hoy.get('Departamento',''), "Lugar": row_hoy.get('Lugar',''),
                            "Coordenadas": row_hoy.get('Coordenadas','')
                        })

                    datos_para_resumen.append({
                        "id_unico": id_unico, "dia": dia, "ruta": ruta_nombre,
                        "puntos": len(df_ruta_hoy), "dist_km": round(props['distance']/1000, 2),
                        "drive_mins": round(props['duration']/60, 0), "color": color_actual,
                        "paradas": paradas_info, "segmentos": segments
                    })

                    fg_trazado = folium.FeatureGroup(name=f"🛣️ Trazado: {ruta_nombre} ({dia})")
                    folium.GeoJson(geojson, style_function=lambda x, c=color_actual: {'color':c, 'weight':4, 'opacity':0.8}).add_to(fg_trazado)

                    for i, (_, row_hoy) in enumerate(df_ruta_hoy.iterrows()):
                        lat, lon = row_hoy['Coords_Procesadas'][1], row_hoy['Coords_Procesadas'][0]
                        popup_txt = f"<b>{i+1}. {row_hoy.get('Lugar','')}</b><br>{row_hoy.get('Departamento','')}"
                        icon_html = f"<div style='background:{color_actual};color:white;border-radius:50%;width:20px;text-align:center;border:1px solid white;font-weight:bold;font-size:10pt'>{i+1}</div>"
                        folium.Marker([lat, lon], popup=popup_txt, icon=folium.DivIcon(html=icon_html)).add_to(fg_trazado)

                    fg_trazado.add_to(mapa_calculado)

        folium.LayerControl(collapsed=True).add_to(mapa_calculado)
        st.session_state['mapa_guardado'] = mapa_calculado
        st.session_state['datos_resumen'] = datos_para_resumen
        st.session_state['calculo_terminado'] = True

# --- VISTA DE RESULTADOS CON PESTAÑAS ---
if st.session_state['calculo_terminado']:
    
    tab_mapa, tab_cronogramas, tab_resumen = st.tabs([
        "🗺️ Mapa Interactivo", 
        "⏱️ Cronogramas Detallados", 
        "📊 Resumen General"
    ])
    
    with tab_mapa:
        st_folium(st.session_state['mapa_guardado'], width=1000, height=650, returned_objects=[], key="map_fix")
    
    data_global_detallada = []
    data_resumen_general = []
        
    with tab_cronogramas:
        for d in st.session_state['datos_resumen']:
            with st.container():
                st.markdown(f"**📍 {d['dia']} | {d['ruta']}** ({d['puntos']} pts | {d['dist_km']} km)")
                
                c1, c2, c3 = st.columns([1.2, 1, 1])
                
                default_h = datetime.time(9,0)
                default_wait = 15
                if "Creación de rutas" in tipo_ruteo:
                    default_h = hora_salida_vrp
                    default_wait = min_parada_vrp
                
                h_inicio = c1.time_input("Salida", default_h, key=f"h_{d['id_unico']}")
                espera = c2.number_input("Espera por parada (min)", 0, 300, default_wait, key=f"w_{d['id_unico']}")
                
                total_espera_calc = espera * (d['puntos'] - 1) if d['puntos'] > 0 else 0
                total_min = d['drive_mins'] + total_espera_calc
                c3.metric("Total Estimado", f"{total_min:.0f} min")
                
                t_actual = datetime.datetime.combine(datetime.date.today(), h_inicio)
                dist_acum = 0.0
                mins_acum = 0.0
                rows_excel = []
                hora_llegada_final = "-"
                
                for i, p in enumerate(d['paradas']):
                    dist_tramo = 0.0
                    mins_tramo = 0.0
                    es_ultimo = (i == len(d['paradas']) - 1)
                    espera_real = 0 if es_ultimo else espera
                    
                    if i > 0:
                        secs = d['segmentos'][i-1]['duration'] if i-1 < len(d['segmentos']) else 0
                        meters = d['segmentos'][i-1]['distance'] if i-1 < len(d['segmentos']) else 0
                        dist_tramo = round(meters/1000, 2)
                        mins_tramo = (secs / 60.0) + espera_real
                        t_actual += datetime.timedelta(seconds=secs)
                    else:
                        mins_tramo = espera_real
                    
                    llegada = t_actual
                    if es_ultimo:
                        hora_llegada_final = llegada.strftime("%H:%M")
                        
                    salida = llegada + datetime.timedelta(minutes=espera_real)
                    t_actual = salida
                    
                    dist_acum += dist_tramo
                    mins_acum += mins_tramo
                    
                    rows_excel.append({
                        "Orden": i+1,
                        "Día": p['Día'], "Ruta": p['Ruta'],
                        "Departamento": p['Departamento'], "Lugar": p['Lugar'],
                        "Coordenadas": p['Coordenadas'],
                        "Llegada": llegada.strftime("%H:%M"),
                        "Salida": salida.strftime("%H:%M") if not es_ultimo else "-",
                        "Minutos Tramo": round(mins_tramo, 0),
                        "Minutos Acumulados": round(mins_acum, 0),
                        "Km Tramo": dist_tramo,
                        "Km Acumulados": round(dist_acum, 2)
                    })
                
                data_global_detallada.extend(rows_excel)
                
                data_resumen_general.append({
                    "Día": d['dia'],
                    "Ruta": d['ruta'],
                    "Hs de Inicio": h_inicio.strftime("%H:%M"),
                    "Hs de Finalización": hora_llegada_final,
                    "Minutos de Demora": total_espera_calc,
                    "Kms Recorridos": round(dist_acum, 2)
                })
                
                with st.expander("Ver Cronograma Detallado"):
                    df_view = pd.DataFrame(rows_excel)
                    st.dataframe(df_view[['Orden','Lugar','Llegada','Salida','Minutos Tramo','Minutos Acumulados','Km Acumulados']], use_container_width=True)
                    
                    bio = io.BytesIO()
                    with pd.ExcelWriter(bio, engine='openpyxl') as w:
                        df_view.to_excel(w, index=False, sheet_name=limpiar_nombre_excel(d['id_unico']))
                    st.download_button("📥 Descargar Excel de esta Ruta", bio.getvalue(), f"Ruta_{limpiar_nombre_excel(d['id_unico'])}.xlsx", key=f"dl_{d['id_unico']}")
                st.divider()

    with tab_resumen:
        st.markdown("### Tabla Resumen Operativo")
        st.info("Este es el resumen general para auditar la eficiencia y horarios reales de finalización de todos los autos.")
        
        df_resumen = pd.DataFrame(data_resumen_general)
        st.dataframe(df_resumen, use_container_width=True)
        
        bio_resumen = io.BytesIO()
        with pd.ExcelWriter(bio_resumen, engine='openpyxl') as w:
            df_resumen.to_excel(w, index=False, sheet_name="Resumen")
        
        col_btn1, col_btn2 = st.columns(2)
        with col_btn1:
            st.download_button("📥 DESCARGAR SOLO RESUMEN", bio_resumen.getvalue(), "Resumen_General.xlsx", type="secondary", use_container_width=True)
            
        if data_global_detallada:
            df_glob = pd.DataFrame(data_global_detallada)
            bio_g = io.BytesIO()
            with pd.ExcelWriter(bio_g, engine='openpyxl') as w:
                df_glob.to_excel(w, index=False, sheet_name="Cronograma Detallado")
                df_resumen.to_excel(w, index=False, sheet_name="Resumen General") 
            with col_btn2:
                st.download_button("📥 DESCARGAR CRONOGRAMA MAESTRO (Completo)", bio_g.getvalue(), "Cronograma_Maestro.xlsx", type="primary", use_container_width=True)
