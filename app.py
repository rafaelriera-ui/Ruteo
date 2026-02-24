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

# --- PROCESAMIENTO INICIAL DEL DATAFRAME ---
df = pd.read_excel(archivo_subido)
df.columns = df.columns.str.strip() 

for col in ['Coordenadas', 'Día']:
    if col not in df.columns:
        st.error(f"❌ No se encontró la columna '{col}' en tu archivo Excel.")
        st.stop()

# Si la columna Ruta no existe o está vacía, la creamos para que no dé error
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

# DEPENDIENDO LA OPCIÓN, MOSTRAMOS DISTINTOS MENÚS
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
    st.sidebar.info("La IA elegirá el mejor punto de inicio y calculará los vehículos necesarios para cumplir el horario y terminar en el Punto Final.")
    
    opciones_lugar_vrp = df_filtrado_dias['Lugar'].unique().tolist() if dias_seleccionados else []
    punto_final_vrp = st.sidebar.selectbox("📍 Punto final del recorrido (Obligatorio):", opciones_lugar_vrp)
    
    col_salida, col_llegada = st.sidebar.columns(2)
    with col_salida:
        hora_salida_vrp = st.time_input("Hora Salida", datetime.time(8, 0))
    with col_llegada:
        hora_llegada_vrp = st.time_input("Límite Llegada", datetime.time(18, 0))
        
    min_parada_vrp = st.sidebar.number_input("Minutos espera por parada", min_value=0, value=15, step=1)
    
    start_dt = datetime.datetime.combine(datetime.date.today(), hora_salida_vrp)
    end_dt = datetime.datetime.combine(datetime.date.today(), hora_llegada_vrp)
    max_time_sec = int((end_dt - start_dt).total_seconds())
    
    if max_time_sec <= 0:
        st.sidebar.error("❌ El horario de llegada debe ser mayor al de salida.")
        st.stop()


# --- BOTÓN DE CÁLCULO ---
if st.sidebar.button("🗺️ Calcular Rutas", type="primary"):
    with st.spinner("Procesando ruteo avanzado (este proceso es complejo y toma unos segundos)..."):
        lat_centro = df_filtrado_dias.iloc[0]['Coords_Procesadas'][1]
        lon_centro = df_filtrado_dias.iloc[0]['Coords_Procesadas'][0]
        mapa_calculado = folium.Map(location=[lat_centro, lon_centro], zoom_start=11)
        
        colores = ['blue', 'red', 'green', 'purple', 'orange', 'darkred', 'cadetblue', 'darkblue', 'pink', 'lightgreen']
        datos_para_resumen = []
        color_idx = 0
        headers = {'Authorization': api_key, 'Content-Type': 'application/json'}

        # ==========================================================
        # LÓGICA 1 Y 2: RUTEO CLÁSICO Y OPTIMIZADO (Respeta Excel)
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
                        url_matrix = 'https://api.openrouteservice.org/v2/matrix/driving-car'
                        resp_matrix = requests.post(url_matrix, json={"locations": lista_coords, "metrics": ["distance"]}, headers=headers)
                        
                        if resp_matrix.status_code == 200:
                            matriz = resp_matrix.json()['distances']
                            num_locs = len(matriz)
                            
                            if punto_final_fijo:
                                end_node = indices_puntos_finales.get(id_unico, num_locs - 1)
                                manager = pywrapcp.RoutingIndexManager(num_locs, 1, [0], [int(end_node)])
                            else:
                                manager = pywrapcp.RoutingIndexManager(num_locs, 1, 0)
                                
                            routing = pywrapcp.RoutingModel(manager)
                            
                            def distance_callback(from_index, to_index):
                                return int(matriz[manager.IndexToNode(from_index)][manager.IndexToNode(to_index)])
                                
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
                            st.error(f"Error API Matriz en {ruta} (Límite max 50 paradas): {resp_matrix.text}")
                            continue

                    url_dirs = 'https://api.openrouteservice.org/v2/directions/driving-car/geojson'
                    body_dirs = {"coordinates": coords_ordenadas, "radiuses": [-1] * len(coords_ordenadas)}
                    resp_dirs = requests.post(url_dirs, json=body_dirs, headers=headers)
                    
                    if resp_dirs.status_code == 200:
                        geojson = resp_dirs.json()
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
                    time.sleep(3)

        # ==========================================================
        # LÓGICA 3: CREACIÓN DE RUTAS PROPIAS (Con Nodo Fantasma)
        # ==========================================================
        elif tipo_ruteo == "Creación de rutas propias":
            destino_row = df_filtrado_dias[df_filtrado_dias['Lugar'] == punto_final_vrp].iloc[0]
            
            for dia in dias_seleccionados:
                df_dia = df[df['Día'] == dia].copy().reset_index(drop=True)
                
                # Garantizamos que el punto final elegido exista en la lista del día
                if punto_final_vrp not in df_dia['Lugar'].values:
                    df_dia = pd.concat([df_dia, destino_row.to_frame().T], ignore_index=True)
                
                end_idx = df_dia[df_dia['Lugar'] == punto_final_vrp].index[0]
                lista_coords = df_dia['Coords_Procesadas'].tolist()
                num_locs = len(lista_coords)
                
                if num_locs < 2: continue
                
                if num_locs > 50:
                    st.error(f"❌ El Día {dia} tiene {num_locs} puntos. La API gratuita permite máximo 50. Por favor, reduce la cantidad.")
                    st.stop()
                    
                dibujar_geozona_circular(lista_coords, f"🌍 DÍA: {dia} (Zona de Reparto)", "black", mapa_calculado)

                url_matrix = 'https://api.openrouteservice.org/v2/matrix/driving-car'
                body_matrix = {"locations": lista_coords, "metrics": ["distance", "duration"]}
                resp_matrix = requests.post(url_matrix, json=body_matrix, headers=headers)
                
                if resp_matrix.status_code == 200:
                    matriz_dist = resp_matrix.json()['distances']
                    matriz_dur = resp_matrix.json()['durations']
                    
                    # MAGIA DEL NODO FANTASMA (Para que la IA decida dónde empezar sin penalización)
                    dummy_idx = num_locs
                    for i in range(num_locs):
                        matriz_dist[i].append(0)
                        matriz_dur[i].append(0)
                    matriz_dist.append([0] * (num_locs + 1))
                    matriz_dur.append([0] * (num_locs + 1))
                    
                    num_vehicles = num_locs # Máximo teórico de autos
                    # Inician en el nodo fantasma, terminan en el nodo elegido por el usuario
                    manager = pywrapcp.RoutingIndexManager(num_locs + 1, num_vehicles, [dummy_idx] * num_vehicles, [int(end_idx)] * num_vehicles)
                    routing = pywrapcp.RoutingModel(manager)
                    
                    def distance_callback(from_index, to_index):
                        return int(matriz_dist[manager.IndexToNode(from_index)][manager.IndexToNode(to_index)])
                    transit_callback_index = routing.RegisterTransitCallback(distance_callback)
                    routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)
                    
                    # Penalización para minimizar el uso de vehículos
                    routing.SetFixedCostOfAllVehicles(100000)
                    
                    def time_callback(from_index, to_index):
                        from_node = manager.IndexToNode(from_index)
                        to_node = manager.IndexToNode(to_index)
                        drive_time = int(matriz_dur[from_node][to_node])
                        # No hay tiempo de espera si es el nodo fantasma o el depósito final
                        wait_time = int(min_parada_vrp * 60) if to_node != dummy_idx and to_node != end_idx else 0
                        return drive_time + wait_time
                    
                    time_callback_index = routing.RegisterTransitCallback(time_callback)
                    routing.AddDimension(
                        time_callback_index,
                        0, max_time_sec, True, "Time"
                    )
                    
                    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
                    search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
                    search_parameters.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
                    search_parameters.time_limit.seconds = 5 # La IA piensa 5 seg por día
                    
                    solution = routing.SolveWithParameters(search_parameters)
                    
                    if solution:
                        vehiculo_real_count = 1
                        for vehicle_id in range(num_vehicles):
                            index = routing.Start(vehicle_id)
                            first_visit = solution.Value(routing.NextVar(index))
                            
                            # Si el vehículo arranca y va directo al final, está vacío
                            if manager.IndexToNode(first_visit) == end_idx:
                                continue 
                            
                            nodos_ordenados = []
                            while not routing.IsEnd(index):
                                node = manager.IndexToNode(index)
                                if node != dummy_idx:
                                    nodos_ordenados.append(node)
                                index = solution.Value(routing.NextVar(index))
                            nodos_ordenados.append(end_idx)
                            
                            coords_ordenadas = [lista_coords[i] for i in nodos_ordenados]
                            
                            ruta_nombre = f"Ruta Auto {vehiculo_real_count}"
                            id_unico = f"{dia} - {ruta_nombre}"
                            vehiculo_real_count += 1
                            color_actual = colores[color_idx % len(colores)]
                            color_idx += 1
                            
                            url_dirs = 'https://api.openrouteservice.org/v2/directions/driving-car/geojson'
                            body_dirs = {"coordinates": coords_ordenadas, "radiuses": [-1] * len(coords_ordenadas)}
                            resp_dirs = requests.post(url_dirs, json=body_dirs, headers=headers)
                            
                            if resp_dirs.status_code == 200:
                                geojson = resp_dirs.json()
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
                            time.sleep(3)
                    else:
                        st.error(f"❌ Imposible cumplir el horario para el día {dia}. El tiempo límite es demasiado corto para visitar todos los puntos y volver al destino final.")
                else:
                    st.error(f"Error API Matriz en Día {dia}: {resp_matrix.text}")

        folium.LayerControl(collapsed=True).add_to(mapa_calculado)
        st.session_state['mapa_guardado'] = mapa_calculado
        st.session_state['datos_resumen'] = datos_para_resumen
        st.session_state['calculo_terminado'] = True

# --- VISTA DE RESULTADOS ---
if st.session_state['calculo_terminado']:
    c_map, c_res = st.columns([1.8, 1.2])
    
    with c_map:
        st_folium(st.session_state['mapa_guardado'], width=800, height=750, returned_objects=[], key="map_fix")
        
    with c_res:
        st.markdown("### ⏱️ Cronograma de Entregas")
        data_global = []
        
        for d in st.session_state['datos_resumen']:
            with st.container():
                st.markdown(f"**{d['dia']} | {d['ruta']}** ({d['puntos']} pts | {d['dist_km']} km)")
                
                c1, c2, c3 = st.columns([1.2, 1, 1])
                
                default_h = datetime.time(9,0)
                default_wait = 15
                if tipo_ruteo == "Creación de rutas propias":
                    default_h = hora_salida_vrp
                    default_wait = min_parada_vrp
                
                h_inicio = c1.time_input("Salida", default_h, key=f"h_{d['id_unico']}")
                espera = c2.number_input("Espera (min)", 0, 300, default_wait, key=f"w_{d['id_unico']}")
                
                # El punto final no suma espera en el cálculo total visible
                total_espera_calc = espera * (d['puntos'] - 1) if d['puntos'] > 0 else 0
                total_min = d['drive_mins'] + total_espera_calc
                c3.metric("Total", f"{total_min:.0f} min")
                
                t_actual = datetime.datetime.combine(datetime.date.today(), h_inicio)
                dist_acum = 0.0
                mins_acum = 0.0
                rows_excel = []
                
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
                
                data_global.extend(rows_excel)
                
                with st.expander("Ver Detalle"):
                    df_view = pd.DataFrame(rows_excel)
                    st.dataframe(df_view[['Orden','Lugar','Llegada','Salida','Minutos Tramo','Minutos Acumulados','Km Acumulados']], use_container_width=True)
                    
                    bio = io.BytesIO()
                    with pd.ExcelWriter(bio, engine='openpyxl') as w:
                        df_view.to_excel(w, index=False, sheet_name=limpiar_nombre_excel(d['ruta']))
                    st.download_button("📥 Excel Ruta", bio.getvalue(), f"Ruta_{limpiar_nombre_excel(d['ruta'])}.xlsx", key=f"dl_{d['id_unico']}")
                st.divider()

        if data_global:
            st.markdown("### 💾 Reporte General")
            df_glob = pd.DataFrame(data_global)
            bio_g = io.BytesIO()
            with pd.ExcelWriter(bio_g, engine='openpyxl') as w:
                df_glob.to_excel(w, index=False, sheet_name="Master")
            st.download_button("📥 DESCARGAR CRONOGRAMA MAESTRO", bio_g.getvalue(), "Cronograma_Completo.xlsx", type="primary", use_container_width=True)
