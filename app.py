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

# --- CONFIGURACIÓN DE LA PÁGINA ---
st.set_page_config(page_title="Gestor de Rutas Logísticas", layout="wide")
st.title("🚚 Gestor de Rutas y Capas")

# --- API KEY PREDETERMINADA ---
api_key = "5b3ce3597851110001cf62480080f4189d6143db946e7c7267b9343d"

# --- INICIALIZAR MEMORIA ESTABLE ---
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

for col in ['Coordenadas', 'Día', 'Ruta']:
    if col not in df.columns:
        st.error(f"❌ No se encontró la columna '{col}' en tu archivo Excel.")
        st.stop()

df['Coords_Procesadas'] = df['Coordenadas'].apply(preparar_coordenadas)
df = df.dropna(subset=['Coords_Procesadas']).copy()

if df.empty:
    st.error("❌ No se encontraron coordenadas válidas en el archivo.")
    st.stop()

# --- BARRA LATERAL: FILTROS ---
st.sidebar.header("1. Filtros de Selección")

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
rutas_disponibles = df_filtrado_dias['Ruta'].unique().tolist()
todas_rutas = st.sidebar.checkbox("✔️ Todas las Rutas", value=True)
if todas_rutas:
    rutas_seleccionadas = rutas_disponibles
else:
    rutas_seleccionadas = st.sidebar.multiselect("Rutas:", rutas_disponibles, default=[])

if not rutas_seleccionadas:
    st.sidebar.warning("Selecciona al menos una ruta.")
    st.stop()

# --- BARRA LATERAL: CONFIGURACIÓN DE RUTEO ---
st.sidebar.markdown("---")
st.sidebar.header("2. Configuración de Ruteo")

tipo_ruteo = st.sidebar.radio(
    "Estrategia de Recorrido:",
    ["Ruteo según Excel (Orden Original)", "Ruteo Optimizado (IA)"]
)

punto_final_fijo = False
indice_punto_final = None

if tipo_ruteo == "Ruteo Optimizado (IA)":
    activar_punto_final = st.sidebar.checkbox("🏁 Definir Punto Final específico")
    
    if activar_punto_final:
        punto_final_fijo = True
        if len(rutas_seleccionadas) == 1 and len(dias_seleccionados) == 1:
            df_unicaruta = df_filtrado_dias[df_filtrado_dias['Ruta'] == rutas_seleccionadas[0]].reset_index(drop=True)
            opciones_lugar = df_unicaruta['Lugar'].tolist()
            lugar_final = st.sidebar.selectbox("Selecciona el destino final:", opciones_lugar, index=len(opciones_lugar)-1)
            indice_punto_final = df_unicaruta[df_unicaruta['Lugar'] == lugar_final].index[0]
        else:
            st.sidebar.info("ℹ️ Al procesar múltiples rutas, se usará el **último punto** de la lista de Excel de cada ruta como destino final.")
            indice_punto_final = -1 

# --- BOTÓN DE CÁLCULO ---
if st.sidebar.button("🗺️ Calcular Rutas", type="primary"):
    with st.spinner("Procesando rutas, distancias y tiempos..."):
        lat_centro = df_filtrado_dias.iloc[0]['Coords_Procesadas'][1]
        lon_centro = df_filtrado_dias.iloc[0]['Coords_Procesadas'][0]
        mapa_calculado = folium.Map(location=[lat_centro, lon_centro], zoom_start=11)
        
        for dia in dias_seleccionados:
            df_dia = df[df['Día'] == dia]
            if not df_dia.empty:
                dibujar_geozona_circular(df_dia['Coords_Procesadas'].tolist(), f"🌍 DÍA: {dia}", "black", mapa_calculado)
        
        colores = ['blue', 'red', 'green', 'purple', 'orange', 'darkred', 'cadetblue']
        datos_para_resumen = []
        color_idx = 0

        for dia in dias_seleccionados:
            for ruta in rutas_seleccionadas:
                df_ruta = df[(df['Día'] == dia) & (df['Ruta'] == ruta)].copy().reset_index(drop=True)
                
                if df_ruta.empty:
                    continue
                
                id_unico = f"{dia} - {ruta}"
                color_actual = colores[color_idx % len(colores)]
                color_idx += 1
                
                lista_coords = df_ruta['Coords_Procesadas'].tolist()
                
                dibujar_geozona_circular(lista_coords, f"🗺️ {ruta}", color_actual, mapa_calculado)
                deptos = df_ruta['Departamento'].unique() if 'Departamento' in df_ruta.columns else []
                for d in deptos:
                    c_depto = df_ruta[df_ruta['Departamento'] == d]['Coords_Procesadas'].tolist()
                    dibujar_geozona_circular(c_depto, f"📍 {d} ({ruta})", color_actual, mapa_calculado, False)

                headers = {'Authorization': api_key, 'Content-Type': 'application/json'}
                
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
                            end_node = indice_punto_final if indice_punto_final != -1 else num_locs - 1
                            manager = pywrapcp.RoutingIndexManager(num_locs, 1, [0], [int(end_node)])
                        else:
                            manager = pywrapcp.RoutingIndexManager(num_locs, 1, 0)
                            
                        routing = pywrapcp.RoutingModel(manager)
                        
                        def distance_callback(from_index, to_index):
                            from_node = manager.IndexToNode(from_index)
                            to_node = manager.IndexToNode(to_index)
                            return int(matriz[from_node][to_node])
                            
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
                        st.error(f"Error API Matriz en {ruta}: {resp_matrix.text}")
                        continue

                url_dirs = 'https://api.openrouteservice.org/v2/directions/driving-car/geojson'
                body_dirs = {"coordinates": coords_ordenadas}
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
                        "puntos": len(df_ruta),
                        "dist_km": round(props['distance']/1000, 2),
                        "drive_mins": round(props['duration']/60, 0),
                        "color": color_actual,
                        "paradas": paradas_info,
                        "segmentos": segments
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
                    st.error(f"Error trazando las calles de {ruta}")

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
                h_inicio = c1.time_input("Salida", datetime.time(9,0), key=f"h_{d['id_unico']}")
                espera = c2.number_input("Espera (min)", 0, 300, 15, key=f"w_{d['id_unico']}")
                
                total_min = d['drive_mins'] + (espera * d['puntos'])
                c3.metric("Total", f"{total_min:.0f} min")
                
                t_actual = datetime.datetime.combine(datetime.date.today(), h_inicio)
                dist_acum = 0.0
                mins_acum = 0.0
                rows_excel = []
                
                for i, p in enumerate(d['paradas']):
                    dist_tramo = 0.0
                    mins_tramo = 0.0
                    
                    if i > 0:
                        secs = d['segmentos'][i-1]['duration'] if i-1 < len(d['segmentos']) else 0
                        meters = d['segmentos'][i-1]['distance'] if i-1 < len(d['segmentos']) else 0
                        dist_tramo = round(meters/1000, 2)
                        mins_tramo = (secs / 60.0) + espera
                        t_actual += datetime.timedelta(seconds=secs)
                    else:
                        mins_tramo = espera
                    
                    llegada = t_actual
                    salida = llegada + datetime.timedelta(minutes=espera)
                    t_actual = salida
                    
                    dist_acum += dist_tramo
                    mins_acum += mins_tramo
                    
                    rows_excel.append({
                        "Orden": i+1,
                        "Día": p['Día'], "Ruta": p['Ruta'],
                        "Departamento": p['Departamento'], "Lugar": p['Lugar'],
                        "Coordenadas": p['Coordenadas'],
                        "Llegada": llegada.strftime("%H:%M"),
                        "Salida": salida.strftime("%H:%M"),
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
