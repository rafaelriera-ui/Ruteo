import streamlit as st
import pandas as pd
import requests
import folium
from streamlit_folium import st_folium
from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp
from haversine import haversine, Unit
import io

# --- CONFIGURACIÓN DE LA PÁGINA ---
st.set_page_config(page_title="Gestor de Rutas Logísticas", layout="wide")
st.title("🚚 Gestor de Rutas y Capas")

# --- API KEY PREDETERMINADA ---
api_key = "5b3ce3597851110001cf62480080f4189d6143db946e7c7267b9343d"

# --- BARRA LATERAL ---
st.sidebar.header("Carga de Datos")
archivo_subido = st.sidebar.file_uploader("Sube tu archivo Excel", type=["xlsx", "xls"])

def preparar_coordenadas(coord_str):
    lat, lon = map(float, str(coord_str).strip().split(','))
    return [lon, lat]

# --- FUNCIÓN: DIBUJAR GEOZONA CIRCULAR ---
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
            location=centro,
            radius=radio_final,
            color=color,
            fill=True,
            fill_color=color,
            fill_opacity=0.15,
            weight=2
        ).add_to(capa)
        capa.add_to(mapa)

# --- LÓGICA PRINCIPAL ---
if archivo_subido is not None:
    df = pd.read_excel(archivo_subido)
    df.columns = df.columns.str.strip() 
    
    if 'Coordenadas' in df.columns:
        df = df[df['Coordenadas'].astype(str).str.contains(',', na=False)].copy()
        df['Coords_Procesadas'] = df['Coordenadas'].apply(preparar_coordenadas)
        
        st.sidebar.header("Gestión de Capas")
        if 'Día' in df.columns:
            dias_disponibles = df['Día'].unique()
            dia_seleccionado = st.sidebar.selectbox("Selecciona la Capa Principal (Día):", dias_disponibles)
            df_dia = df[df['Día'] == dia_seleccionado]
            
            if 'Ruta' in df_dia.columns:
                rutas_disponibles = df_dia['Ruta'].unique()
                rutas_seleccionadas = st.sidebar.multiselect("Selecciona las Subcapas (Rutas) a calcular:", rutas_disponibles)
                
                # EL BOTÓN AHORA SOLO GUARDA DATOS EN LA MEMORIA
                if st.button("🗺️ Calcular y Generar Mapa"):
                    if not rutas_seleccionadas:
                        st.warning("Selecciona al menos una ruta.")
                    else:
                        with st.spinner("Calculando geozonas circulares y trazados óptimos..."):
                            lat_centro_ini = df_dia.iloc[0]['Coords_Procesadas'][1]
                            lon_centro_ini = df_dia.iloc[0]['Coords_Procesadas'][0]
                            mapa_calculado = folium.Map(location=[lat_centro_ini, lon_centro_ini], zoom_start=11)
                            
                            lista_dia_completo = df_dia['Coords_Procesadas'].tolist()
                            dibujar_geozona_circular(lista_dia_completo, f"🌍 GEOZONA DÍA: {dia_seleccionado}", "black", mapa_calculado, mostrar_por_defecto=True)
                            
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
                                            
                                            datos_para_resumen.append({
                                                "ruta": ruta,
                                                "puntos": num_puntos,
                                                "dist_km": round(propiedades['distance'] / 1000, 2),
                                                "drive_mins": round(propiedades['duration'] / 60, 0),
                                                "color": color_actual
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

                            folium.LayerControl(collapsed=False).add_to(mapa_calculado)
                            
                            # GUARDAMOS LOS RESULTADOS EN LA MEMORIA DE LA SESIÓN
                            st.session_state['mapa_guardado'] = mapa_calculado
                            st.session_state['datos_resumen'] = datos_para_resumen

                # ESTA SECCIÓN DIBUJA LA PANTALLA USANDO LA MEMORIA (Así no se borra al cambiar números)
                if 'mapa_guardado' in st.session_state and 'datos_resumen' in st.session_state:
                    col_mapa, col_resumen = st.columns([2, 1])
                    
                    with col_mapa:
                        st.subheader("Mapa Interactivo")
                        st_folium(st.session_state['mapa_guardado'], width=800, height=650, returned_objects=[])
                        
                    with col_resumen:
                        st.subheader("Resumen y Tiempos")
                        st.write("Ajusta los minutos de espera por parada:")
                        
                        datos_para_excel = [] # Lista que llenaremos para armar el Excel
                        
                        for datos in st.session_state['datos_resumen']:
                            st.markdown(f"---")
                            st.markdown(f"### 📍 Ruta: {datos['ruta']}")
                            
                            # Input interactivo
                            min_parada = st.number_input(
                                f"Minutos 'muertos' por parada en {datos['ruta']}:", 
                                min_value=0, value=0, step=1, 
                                key=f"stop_time_{datos['ruta']}"
                            )
                            
                            tiempo_total_paradas = min_parada * datos['puntos']
                            tiempo_total_ruta = datos['drive_mins'] + tiempo_total_paradas
                            
                            c1, c2 = st.columns(2)
                            c1.metric("Cantidad de Puntos", datos['puntos'])
                            c2.metric("Distancia Total", f"{datos['dist_km']} km")
                            
                            st.metric(
                                label="⏱️ TIEMPO TOTAL ESTIMADO", 
                                value=f"{tiempo_total_ruta:.0f} min",
                                delta=f"{datos['drive_mins']:.0f} min manejo + {tiempo_total_paradas} min espera",
                                delta_color="off"
                            )
                            
                            # Guardamos la info calculada para el Excel
                            datos_para_excel.append({
                                "Ruta": datos['ruta'],
                                "Puntos a visitar": datos['puntos'],
                                "Distancia (Km)": datos['dist_km'],
                                "Manejo Estimado (Minutos)": datos['drive_mins'],
                                "Minutos de Espera Totales": tiempo_total_paradas,
                                "Tiempo Total del Recorrido (Min)": tiempo_total_ruta
                            })

                        # BOTÓN DE DESCARGA EN EXCEL
                        if len(datos_para_excel) > 0:
                            st.markdown("---")
                            df_excel = pd.DataFrame(datos_para_excel)
                            
                            # Crear el archivo Excel en la memoria invisible
                            buffer = io.BytesIO()
                            with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
                                df_excel.to_excel(writer, index=False, sheet_name='Tiempos Logistica')
                            
                            st.download_button(
                                label="📥 Descargar Resumen en Excel",
                                data=buffer.getvalue(),
                                file_name="Resumen_Rutas_Tiempos.xlsx",
                                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                            )

else:
    st.info("👈 Por favor, sube tu archivo Excel en la barra lateral para comenzar.")
