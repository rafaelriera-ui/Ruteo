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

# --- API KEY PREDETERMINADA ---
api_key = "5b3ce3597851110001cf62480080f4189d6143db946e7c7267b9343d"

if 'calculo_terminado' not in st.session_state:
    st.session_state['calculo_terminado'] = False

# --- BARRA LATERAL: CARGA ---
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

def obtener_matriz_masiva(lista_coords, headers):
    if len(lista_coords) <= 50:
        url_matrix = 'https://api.openrouteservice.org/v2/matrix/driving-car'
        body_matrix = {"locations": lista_coords, "metrics": ["distance", "duration"]}
        resp = requests.post(url_matrix, json=body_matrix, headers=headers)
        if resp.status_code == 200:
            data = resp.json()
            return data['distances'], data['durations'], None
        else:
            return None, None, resp.text
    else:
        matriz_dist = []
        matriz_dur = []
        vel_ms = 6.94 
        for p1 in lista_coords:
            f_dist = []
            f_dur = []
            for p2 in lista_coords:
                dist_m = haversine((p1[1], p1[0]), (p2[1], p2[0]), unit=Unit.METERS) * 1.3
                f_dist.append(dist_m)
                f_dur.append(dist_m / vel_ms)
            matriz_dist.append(f_dist)
            matriz_dur.append(f_dur)
        return matriz_dist, matriz_dur, None

def obtener_trazado_masivo(coords_ordenadas, headers):
    total_dist = 0
    total_dur = 0
    all_segments = []
    merged_coordinates = []
    
    chunk_size = 40
    for i in range(0, len(coords_ordenadas) - 1, chunk_size - 1):
        chunk = coords_ordenadas[i:i + chunk_size]
        if len(chunk) < 2: break
        
        url_dirs = 'https://api.openrouteservice.org/v2/directions/driving-car/geojson'
        body_dirs = {"coordinates": chunk, "radiuses": [-1] * len(chunk)}
        resp = requests.post(url_dirs, json=body_dirs, headers=headers)
        
        if resp.status_code == 200:
            data = resp.json()
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
            return None, resp.text
            
        time.sleep(1.5)
            
    fake_geojson = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {
                "summary": {"distance": total_dist, "duration": total_dur},
                "segments": all_segments
            },
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

tipo_ruteo = st.sidebar.radio(
    "Selecciona cómo armar las rutas:",
    ["Ruteo según Excel (Orden Original)", "Ruteo Optimizado (IA)", "Creación de rutas propias"]
)

punto_final_fijo = False
indices_puntos_finales = {}
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
        activar_punto_final = st.sidebar.checkbox("🏁 Definir Punto Final específico")
        if activar_punto_final:
            punto_final_fijo = True
            st.sidebar.markdown("**Selecciona el destino final para cada ruta:**")
            for dia in dias_seleccionados:
                for ruta in rutas_seleccionadas:
                    df_unicaruta = df_filtrado_dias[(df_filtrado_dias['Día'] == dia) & (df_filtrado_dias['Ruta'] == ruta)].reset_index(drop=True)
                    if not df_unicaruta.empty:
                        opciones_lugar = df_unicaruta['Lugar'].tolist()
                        id_ruta = f"{dia} - {ruta}"
                        lugar_final = st.sidebar.selectbox(f"Destino para {id_ruta}:", opciones_lugar, index=len(opciones_lugar)-1, key=f"end_{id_ruta}")
                        indices_puntos_finales[id_ruta] = df_unicaruta[df_unicaruta['Lugar'] == lugar_final].index[0]

elif tipo_ruteo == "Creación de rutas propias":
    st.sidebar.markdown("---")
    st.sidebar.header("Configuración de Flota Automática")
    st.sidebar.info("La IA exprimirá al máximo el horario límite (ej: 14:30) para usar la menor cantidad posible de vehículos.")
    
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
    with st.spinner("IA estrujando vehículos al máximo... (Esto tomará unos 15 segundos para garantizar la menor flota posible)"):
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
                        matriz_dist, matriz_dur, err_matriz = obtener_matriz_masiva(lista_coords, headers)
                        if not err_matriz:
                            num_locs = len(matriz_dist)
                            
                            if punto_final_fijo:
                                end_node = indices_puntos_finales.get(id_unico, num_locs - 1)
                                manager = pywrapcp.RoutingIndexManager(num_locs, 1, [0], [int(end_node)])
                            else:
                                manager = pywrapcp.RoutingIndexManager(num_locs, 1, 0)
                                
                            routing = pywrapcp.RoutingModel(manager)
                            
                            def distance_callback(from_index, to_index):
                                return int(matriz_dist[manager.IndexToNode(from_index)][manager.IndexToNode(to_index)])
                            transit_callback_index = routing.RegisterTransitCallback(distance_callback)
                            routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)
                            
                            search_parameters = pywrapcp.DefaultRoutingSearchParameters()
                            search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
                            solution = routing.SolveWithParameters(search_parameters)
                            
                            if solution:
                                index = routing.Start(0)
                                while not routing.IsEnd(index):
                                    nodos_ordenados.append(manager.IndexToNode(index))
                                    index = solution.Value(routing.NextVar(index))
                                if punto_final_fijo:
                                    nodos_ordenados.append(manager.IndexToNode(index))
                                coords_ordenadas = [lista_coords[i] for i in nodos_ordenados]
                            else:
                                st.error(f"No se encontró solución de optimización para {ruta}")
                                continue
                        else:
                            st.error(f"Error Matriz en {ruta}: {err_matriz}")
                            continue

                    geojson, err_dirs = obtener_trazado_masivo(coords_ordenadas, headers)
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
        # LÓGICA 3: CREACIÓN DE RUTAS PROPIAS MASIVAS (AHORRO EXTREMO DE FLOTA)
        # ==========================================================
        elif tipo_ruteo == "Creación de rutas propias":
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
                
                if not err_matriz:
                    dummy_idx = num_locs
                    for i in range(num_locs):
                        matriz_dist[i].append(0)
                        matriz_dur[i].append(0)
                    matriz_dist.append([0] * (num_locs + 1))
                    matriz_dur.append([0] * (num_locs + 1))
                    
                    num_vehicles = num_locs 
                    manager = pywrapcp.RoutingIndexManager(num_locs + 1, num_vehicles, [dummy_idx] * num_vehicles, [int(end_idx)] * num_vehicles)
                    routing = pywrapcp.RoutingModel(manager)
                    
                    # 1. FUNCIÓN DE DISTANCIA CON "IMPUESTO DE ARRANQUE"
                    def distance_callback(from_index, to_index):
                        from_node = manager.IndexToNode(from_index)
                        to_node = manager.IndexToNode(to_index)
                        dist = int(matriz_dist[from_node][to_node])
                        
                        # Cada vez que un vehículo sale del "punto fantasma" hacia el trabajo real, le cobramos 50 MILLONES.
                        # Esto obliga a la IA a usar la menor cantidad de autos posibles.
                        if from_node == dummy_idx and to_node != end_idx:
                            return dist + 50000000 
                        return dist
                        
                    transit_callback_index = routing.RegisterTransitCallback(distance_callback)
                    routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)
                    
                    def time_callback(from_index, to_index):
                        from_node = manager.IndexToNode(from_index)
                        to_node = manager.IndexToNode(to_index)
                        drive_time = int(matriz_dur[from_node][to_node])
                        wait_time = int(min_parada_vrp * 60) if to_node != dummy_idx and to_node != end_idx else 0
                        return drive_time + wait_time
                        
                    time_callback_index = routing.RegisterTransitCallback(time_callback)
                    
                    # 2. EL MURO DE HORMIGÓN DEL HORARIO: Límite estricto de llegada en max_time_sec (ej 14:30)
                    routing.AddDimension(time_callback_index, 0, max_time_sec, True, "Time")
                    
                    # 3. ANTI-COLAPSO: Penalización GIGANTE por descartar puntos. Preferirá usar un auto extra a descartar algo.
                    penalty_drop = 100000000 
                    for node in range(num_locs + 1):
                        if node != dummy_idx and node != end_idx:
                            routing.AddDisjunction([manager.NodeToIndex(node)], penalty_drop)

                    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
                    # PATH_CHEAPEST_ARC obliga a la IA a "llenar a tope" un auto antes de pasar al siguiente.
                    search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
                    search_parameters.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
                    search_parameters.time_limit.seconds = 15 
                    
                    solution = routing.SolveWithParameters(search_parameters)
                    
                    if solution:
                        vehiculo_real_count = 1
                        nodos_visitados_totales = 0
                        
                        for vehicle_id in range(num_vehicles):
                            index = routing.Start(vehicle_id)
                            first_visit = solution.Value(routing.NextVar(index))
                            
                            if manager.IndexToNode(first_visit) == end_idx:
                                continue 
                            
                            nodos_ordenados = []
                            while not routing.IsEnd(index):
                                node = manager.IndexToNode(index)
                                if node != dummy_idx:
                                    nodos_ordenados.append(node)
                                    nodos_visitados_totales += 1
                                index = solution.Value(routing.NextVar(index))
                            nodos_ordenados.append(end_idx)
                            
                            coords_ordenadas = [lista_coords[i] for i in nodos_ordenados]
                            
                            ruta_nombre = f"Auto {vehiculo_real_count}"
                            id_unico = f"{dia} - {ruta_nombre}"
                            vehiculo_real_count += 1
                            color_actual = colores[color_idx % len(colores)]
                            color_idx += 1
                            
                            geojson, err_dirs = obtener_trazado_masivo(coords_ordenadas, headers)
                            
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
                        
                        if nodos_visitados_totales < num_locs - 1:
                            faltantes = (num_locs - 1) - nodos_visitados_totales
                            st.warning(f"⚠️ En el {dia}, {faltantes} puntos estaban tan aislados geográficamente que ni siquiera dedicándoles un auto exclusivo logran volver antes del límite de las {hora_llegada_vrp.strftime('%H:%M')}.")
                    else:
                        st.error(f"❌ Imposible generar rutas para {dia}. Error de cálculo.")
                else:
                    st.error(f"Error Matriz {dia}: {err_matriz}")

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
                if tipo_ruteo == "Creación de rutas propias":
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
