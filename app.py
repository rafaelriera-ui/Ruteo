import streamlit as st
import pandas as pd
import requests
import folium
from streamlit_folium import st_folium
from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp

# --- CONFIGURACIÓN DE LA PÁGINA ---
st.set_page_config(page_title="Gestor de Rutas Logísticas", layout="wide")
st.title("🚚 Gestor de Rutas y Capas")

# --- BARRA LATERAL (CONFIGURACIÓN Y FILTROS) ---
st.sidebar.header("1. Configuración")
# Pedimos la clave por pantalla para no dejarla expuesta en el código público
api_key = st.sidebar.text_input("Ingresa tu API Key de OpenRouteService", type="password")

st.sidebar.header("2. Carga de Datos")
archivo_subido = st.sidebar.file_uploader("Sube tu archivo Excel", type=["xlsx", "xls"])

def preparar_coordenadas(coord_str):
    lat, lon = map(float, str(coord_str).strip().split(','))
    return [lon, lat]

# --- LÓGICA PRINCIPAL ---
if archivo_subido is not None and api_key:
    # Leer y limpiar datos
    df = pd.read_excel(archivo_subido)
    df = df[df['Coordenadas'].astype(str).str.contains(',', na=False)].copy()
    df['Coords_Procesadas'] = df['Coordenadas'].apply(preparar_coordenadas)
    
    st.sidebar.header("3. Gestión de Capas")
    
    # Filtro CAPA PRINCIPAL (Día)
    dias_disponibles = df['Día'].unique()
    dia_seleccionado = st.sidebar.selectbox("Selecciona la Capa Principal (Día):", dias_disponibles)
    
    df_dia = df[df['Día'] == dia_seleccionado]
    
    # Filtro SUBCAPA (Ruta)
    rutas_disponibles = df_dia['Ruta'].unique()
    rutas_seleccionadas = st.sidebar.multiselect("Selecciona las Subcapas (Rutas) a calcular:", rutas_disponibles)
    
    if st.button("🗺️ Calcular y Generar Mapa"):
        if not rutas_seleccionadas:
            st.warning("Por favor, selecciona al menos una ruta para calcular.")
        else:
            with st.spinner("Calculando las rutas óptimas por las calles..."):
                
                # Crear el mapa base centrado en el primer punto del día
                lat_centro = df_dia.iloc[0]['Coords_Procesadas'][1]
                lon_centro = df_dia.iloc[0]['Coords_Procesadas'][0]
                mapa = folium.Map(location=[lat_centro, lon_centro], zoom_start=12)
                
                # Definir colores para diferenciar las rutas en el mapa
                colores_rutas = ['blue', 'red', 'green', 'purple', 'orange', 'darkred', 'cadetblue']
                
                resumen_resultados = []

                # Procesar cada ruta seleccionada
                for i, ruta in enumerate(rutas_seleccionadas):
                    df_ruta = df_dia[df_dia['Ruta'] == ruta].copy().reset_index(drop=True)
                    lista_coordenadas = df_ruta['Coords_Procesadas'].tolist()
                    color_actual = colores_rutas[i % len(colores_rutas)]
                    
                    # 1. Obtener Matriz
                    url_matriz = 'https://api.openrouteservice.org/v2/matrix/driving-car'
                    headers = {'Authorization': api_key, 'Content-Type': 'application/json'}
                    body_matriz = {"locations": lista_coordenadas, "metrics": ["distance"]}
                    response_matriz = requests.post(url_matriz, json=body_matriz, headers=headers)
                    
                    if response_matriz.status_code == 200:
                        matriz = response_matriz.json()['distances']
                        
                        # 2. OR-Tools
                        manager = pywrapcp.RoutingIndexManager(len(matriz), 1, 0)
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
                            nodos_ordenados = []
                            while not routing.IsEnd(index):
                                nodos_ordenados.append(manager.IndexToNode(index))
                                index = solution.Value(routing.NextVar(index))
                            
                            coords_ordenadas = [lista_coordenadas[j] for j in nodos_ordenados]
                            
                            # 3. Obtener Trazado por Calles
                            url_rutas = 'https://api.openrouteservice.org/v2/directions/driving-car/geojson'
                            body_rutas = {"coordinates": coords_ordenadas}
                            response_rutas = requests.post(url_rutas, json=body_rutas, headers=headers)
                            
                            if response_rutas.status_code == 200:
                                geojson_ruta = response_rutas.json()
                                propiedades = geojson_ruta['features'][0]['properties']['summary']
                                
                                # Guardar resultados para la tabla
                                resumen_resultados.append({
                                    "Ruta": ruta,
                                    "Distancia (km)": round(propiedades['distance'] / 1000, 2),
                                    "Tiempo (min)": round(propiedades['duration'] / 60, 0)
                                })
                                
                                # --- CREAR CAPA INDEPENDIENTE (FeatureGroup) ---
                                capa_ruta = folium.FeatureGroup(name=f"Ruta: {ruta} ({color_actual})")
                                
                                # Agregar trazado a la capa
                                folium.GeoJson(
                                    geojson_ruta, 
                                    style_function=lambda x, color=color_actual: {'color': color, 'weight': 5, 'opacity': 0.8}
                                ).add_to(capa_ruta)
                                
                                # Agregar marcadores a la capa
                                for paso, nodo in enumerate(nodos_ordenados):
                                    fila = df_ruta.iloc[nodo]
                                    lat, lon = fila['Coords_Procesadas'][1], fila['Coords_Procesadas'][0]
                                    
                                    # Info de las subcapas para el popup
                                    popup_html = f"<b>Orden:</b> {paso+1}<br><b>Día:</b> {fila['Día']}<br><b>Ruta:</b> {fila['Ruta']}<br><b>Depto:</b> {fila['Departamento']}<br><b>Lugar:</b> {fila['Lugar']}"
                                    
                                    icono = folium.DivIcon(html=f"""
                                        <div style="font-size: 10pt; font-weight: bold; color: white; background-color: {color_actual}; 
                                        border-radius: 50%; width: 20px; height: 20px; text-align: center; border: 2px solid white; box-shadow: 1px 1px 3px rgba(0,0,0,0.5);">
                                        {paso + 1}
                                        </div>""")
                                    
                                    folium.Marker([lat, lon], popup=popup_html, icon=icono).add_to(capa_ruta)
                                
                                # Añadir la capa terminada al mapa principal
                                capa_ruta.add_to(mapa)

                # Agregar el control interactivo de capas
                folium.LayerControl().add_to(mapa)

                # Mostrar Resultados
                col1, col2 = st.columns([2, 1])
                
                with col1:
                    st.markdown("### Mapa Interactivo (Usa el control arriba a la derecha para prender/apagar Rutas)")
                    st_folium(mapa, width=800, height=600, returned_objects=[])
                
                with col2:
                    st.markdown("### Resumen Operativo")
                    st.dataframe(pd.DataFrame(resumen_resultados))

elif not api_key:
    st.info("👈 Por favor, ingresa tu API Key en la barra lateral para comenzar.")
elif archivo_subido is None:
    st.info("👈 Por favor, sube tu archivo Excel en la barra lateral para comenzar.")
