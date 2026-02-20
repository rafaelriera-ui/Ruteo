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

# --- BARRA LATERAL ---
st.sidebar.header("Carga de Datos")
archivo_subido = st.sidebar.file_uploader("Sube tu archivo Excel", type=["xlsx", "xls"])

if archivo_subido is None:
    st.info("👈 Por favor, sube tu archivo Excel en la barra lateral para comenzar.")
    st.stop()

# --- FUNCIONES ---
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
    """Limpia caracteres inválidos para crear hojas de Excel sin error"""
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

# --- LÓGICA PRINCIPAL ---
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

st.sidebar.header("Gestión de Capas")
dias_disponibles = df['Día'].unique()
# MEJORA 1: Multiselect para Días
dias_seleccionados = st.sidebar.multiselect("Selecciona la Capa Principal (Día):", dias_disponibles, default=[dias_disponibles[0]] if len(dias_disponibles)>0 else None)

if not dias_seleccionados:
    st.sidebar.warning("Selecciona al menos un Día.")
    st.stop()

df_dia = df[df['Día'].isin(dias_seleccionados)]

rutas_disponibles = df_dia['Ruta'].unique()
rutas_seleccionadas = st.sidebar.multiselect("Selecciona las Subcapas (Rutas) a calcular:", rutas_disponibles)

# --- EL BOTÓN DE CÁLCULO ---
if st.sidebar.button("🗺️ Calcular y Generar Mapa", type="primary"):
    if not rutas_seleccionadas:
        st.sidebar.warning("Selecciona al menos una ruta.")
        st.stop()
        
    with st.spinner("Calculando geozonas circulares y trazados óptimos..."):
        lat_centro_ini = df_dia.iloc[0]['Coords_Procesadas'][1]
        lon_centro_ini = df_dia.iloc[0]['Coords_Procesadas'][0]
        mapa_calculado = folium.Map(location=[lat_centro_ini, lon_centro_ini], zoom_start=11)
        
        # Geozonas para TODOS los días seleccionados
        for dia in dias_seleccionados:
            df_este_dia = df_dia[df_dia['Día'] == dia]
            lista_este_dia = df_este_dia['Coords_Procesadas'].tolist()
            dibujar_geozona_circular(lista_este_dia, f"🌍 GEOZONA DÍA: {dia}", "black", mapa_calculado, mostrar_por_defecto=True)
        
        colores_rutas = ['blue', 'red', 'green', 'purple', 'orange', 'darkred', 'cadetblue']
        datos_para_resumen = []

        for i, ruta in enumerate(rutas_seleccionadas):
            df_ruta = df_dia[df_dia['Ruta'] == ruta].copy().reset_index(drop=True)
            lista_coordenadas = df_ruta['Coords_Procesadas'].tolist()
            color_actual = colores_rutas[i % len(colores_rutas)]
            num_puntos = len(df_ruta)
            
            dibujar_geozona_circular(lista_coordenadas, f"🗺️ Geozona Ruta: {ruta}", color_actual, mapa_calculado, mostrar_por_defecto=True)
            
            deptos_en_ruta = df_ruta['Departamento'].unique() if 'Departamento' in df_ruta.columns else []
            for depto in deptos_en_ruta:
                coords_depto = df_ruta[df_ruta['Departamento'] == depto]['Coords_Procesadas'].tolist()
                dibujar_geozona_circular(coords_depto, f"    📍 Geozona Depto: {depto} (Ruta {ruta})", color_actual, mapa_calculado, mostrar_por_defecto=False)

            url_matriz = 'https://api.openrouteservice.org/v2/matrix/driving-car'
            headers = {'Authorization': api_key, 'Content-Type': 'application/json'}
            response_matriz = requests.post(url_matriz, json={"locations": lista_coordenadas, "metrics": ["distance"]}, headers=headers)
            
            if response_matriz.status_code == 200:
                matriz = response_matriz.json()['distances']
                manager = pywrapcp.RoutingIndexManager(len(matriz), 1, 0)
                routing = pywrapcp.RoutingModel(manager)
                def distance_callback(from_index, to_index): return int(matriz[manager.IndexToNode(from_index)][manager.IndexToNode(to_index)])
                transit_callback_index = routing.RegisterTransitCallback(distance_callback)
                routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)
                search_parameters = pywrapcp.DefaultRoutingSearchParameters()
                search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
                solution = routing.SolveWithParameters(search_parameters)
                
                if solution:
                    index = routing.Start(0)
                    nodos_ordenados = []
                    while not routing.IsEnd(index):
                        nodos_ordenados.append(manager.IndexToNode(index))
                        index = solution.Value(routing.NextVar(index))
                    
                    coords_ordenadas = [lista_coordenadas[j] for j in nodos_ordenados]
                    response_rutas = requests.post('https://api.openrouteservice.org/v2/directions/driving-car/geojson', json={"coordinates": coords_ordenadas}, headers=headers)
                    
                    if response_rutas.status_code == 200:
                        geojson_ruta = response_rutas.json()
                        propiedades = geojson_ruta['features'][0]['properties']['summary']
                        segmentos = geojson_ruta['features'][0]['properties'].get('segments', [])
                        
                        paradas_ordenadas = []
                        for paso, nodo in enumerate(nodos_ordenados):
                            fila = df_ruta.iloc[nodo]
                            paradas_ordenadas.append({
                                "Día": fila.get('Día', ''),
                                "Ruta": fila.get('Ruta', ''),
                                "Departamento": fila.get('Departamento', ''),
                                "Lugar": fila.get('Lugar', ''),
                                "Coordenadas": fila.get('Coordenadas', '')
                            })
                        
                        datos_para_resumen.append({
                            "ruta": ruta,
                            "puntos": num_puntos,
                            "dist_km": round(propiedades['distance'] / 1000, 2),
                            "drive_mins": round(propiedades['duration'] / 60, 0),
                            "color": color_actual,
                            "paradas": paradas_ordenadas,
                            "segmentos": segmentos
                        })
                        
                        capa_trazado = folium.FeatureGroup(name=f"🛣️ Trazado: {ruta}")
                        folium.GeoJson(geojson_ruta, style_function=lambda x, c=color_actual: {'color': c, 'weight': 4, 'opacity': 0.9}).add_to(capa_trazado)
                        
                        for paso, nodo in enumerate(nodos_ordenados):
                            fila = df_ruta.iloc[nodo]
                            lat, lon = fila['Coords_Procesadas'][1], fila['Coords_Procesadas'][0]
                            popup_html = f"<b>Orden:</b> {paso+1}<br><b>Día:</b> {fila.get('Día', '')}<br><b>Ruta:</b> {fila.get('Ruta', '')}<br><b>Depto:</b> {fila.get('Departamento', '')}<br><b>Lugar:</b> {fila.get('Lugar', '')}"
                            icono = folium.DivIcon(html=f"<div style='font-size: 10pt; font-weight: bold; color: white; background-color: {color_actual}; border-radius: 50%; width: 20px; height: 20px; text-align: center; border: 2px solid white; box-shadow: 2px 2px 4px rgba(0,0,0,0.4);'>{paso + 1}</div>")
                            folium.Marker([lat, lon], popup=popup_html, icon=icono).add_to(capa_trazado)
                        
                        capa_trazado.add_to(mapa_calculado)

        # MEJORA 2: El control de capas nace cerrado para no bloquear la pantalla
        folium.LayerControl(collapsed=True).add_to(mapa_calculado)
        
        st.session_state['mapa_guardado'] = mapa_calculado
        st.session_state['datos_resumen'] = datos_para_resumen
        st.session_state['calculo_terminado'] = True

# --- PANTALLA FIJA ---
if st.session_state['calculo_terminado']:
    col_mapa, col_resumen = st.columns([1.8, 1.2])
    
    with col_mapa:
        st_folium(st.session_state['mapa_guardado'], width=800, height=750, returned_objects=[], key="mapa_fijo")
        
    with col_resumen:
        st.markdown("### Cronograma Dinámico")
        todos_los_cronogramas = [] 
        
        for datos in st.session_state['datos_resumen']:
            with st.container():
                st.markdown(f"**📍 Ruta: {datos['ruta']}** | 📦 {datos['puntos']} paradas | 🛣️ {datos['dist_km']} km")
                
                # MEJORA 4: Diseño Minimalista en una sola fila
                c1, c2, c3 = st.columns([1.2, 1, 1.2])
                with c1:
                    # MEJORA 3: Horario 09:00 por defecto
                    hora_inicio = st.time_input("Salida", value=datetime.time(9, 0), key=f"start_{datos['ruta']}")
                with c2:
                    min_parada = st.number_input("Espera (min)", min_value=0, value=0, step=1, key=f"stop_{datos['ruta']}")
                
                tiempo_total_paradas = min_parada * datos['puntos']
                tiempo_total_ruta = datos['drive_mins'] + tiempo_total_paradas
                
                with c3:
                    st.metric("Total", f"{tiempo_total_ruta:.0f} min")
                
                # Cálculos internos del cronograma
                fecha_base = datetime.datetime.combine(datetime.date.today(), hora_inicio)
                tiempo_actual = fecha_base
                salida_anterior = fecha_base
                cronograma_ruta = []
                
                for idx, parada in enumerate(datos['paradas']):
                    if idx == 0:
                        llegada = tiempo_actual
                    else:
                        segundos_manejo = datos['segmentos'][idx-1]['duration'] if len(datos['segmentos']) > idx-1 else 0
                        llegada = salida_anterior + datetime.timedelta(seconds=segundos_manejo)
                    
                    salida = llegada + datetime.timedelta(minutes=min_parada)
                    salida_anterior = salida 
                    
                    cronograma_ruta.append({
                        "Día": parada['Día'], "Ruta": parada['Ruta'],
                        "Departamento": parada['Departamento'], "Lugar": parada['Lugar'],
                        "Coordenadas": parada['Coordenadas'], "Llegada": llegada.strftime("%H:%M"),
                        "Salida": salida.strftime("%H:%M")
                    })
                    
                todos_los_cronogramas.extend(cronograma_ruta)

                with st.expander("Ver Detalle / Descargar"):
                    df_ruta_detallada = pd.DataFrame(cronograma_ruta)
                    st.dataframe(df_ruta_detallada, use_container_width=True)
                    
                    buffer_indiv = io.BytesIO()
                    with pd.ExcelWriter(buffer_indiv, engine='openpyxl') as writer:
                        # MEJORA 5: Limpieza de nombres problemáticos para el Excel
                        nombre_hoja_seguro = limpiar_nombre_excel(datos['ruta'])
                        df_ruta_detallada.to_excel(writer, index=False, sheet_name=nombre_hoja_seguro)
                    
                    st.download_button(
                        label="📥 Descargar", data=buffer_indiv.getvalue(),
                        file_name=f"Cronograma_{limpiar_nombre_excel(datos['ruta'])}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        key=f"dl_{datos['ruta']}", use_container_width=True
                    )
                st.divider()

    if len(todos_los_cronogramas) > 0:
        st.markdown("### 💾 Exportación Global")
        df_global = pd.DataFrame(todos_los_cronogramas)
        buffer_global = io.BytesIO()
        with pd.ExcelWriter(buffer_global, engine='openpyxl') as writer:
            df_global.to_excel(writer, index=False, sheet_name='Cronograma Maestro')
        
        st.download_button(
            label="📥 DESCARGAR CRONOGRAMA MAESTRO (Todas las rutas unificadas)",
            data=buffer_global.getvalue(),
            file_name="Cronograma_Logistica_Completo.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary", use_container_width=True
        )
