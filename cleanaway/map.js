'use strict';
const $=id=>document.getElementById(id);
const money=n=>new Intl.NumberFormat('en-AU',{style:'currency',currency:'AUD',notation:'compact',maximumFractionDigits:1}).format(n);
const number=n=>new Intl.NumberFormat('en-AU').format(n);
const pct=n=>(n*100).toFixed(1)+'%';
let geography,features=[],rows=new Map(),selected=null,hover=null,ready=false;
function readRows(){
  rows=new Map();
  const raw=new URLSearchParams(location.hash.slice(1)).get('rows')||'';
  for(const entry of raw.split(';')){
    if(!entry)continue;
    const a=entry.split(',');
    if(a.length!==5||!/^\d{5}$/.test(a[0]))continue;
    const [revenue,margin,lines,orders]=a.slice(1).map(Number);
    if(![revenue,margin,lines,orders].every(Number.isFinite)||revenue<=0)continue;
    rows.set(a[0],{revenue,margin,gm:margin/revenue,lines,orders});
  }
}
readRows();
const protocol=new pmtiles.Protocol();
maplibregl.addProtocol('pmtiles',protocol.tile);
const baseLayers=basemaps.layers('protomaps',basemaps.namedFlavor('dark'),{lang:'en'});
const map=new maplibregl.Map({
  container:'map',center:[134,-28],zoom:3,minZoom:2,maxZoom:13,renderWorldCopies:false,attributionControl:false,
  style:{version:8,glyphs:'https://protomaps.github.io/basemaps-assets/fonts/{fontstack}/{range}.pbf',sprite:'https://protomaps.github.io/basemaps-assets/sprites/v4/dark',sources:{protomaps:{type:'vector',url:'pmtiles://https://oscar-omni.github.io/oscar-omni/cleanaway/australia.pmtiles',attribution:'<a href="https://protomaps.com" target="_blank" rel="noopener">Protomaps</a> · <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">© OpenStreetMap</a>'}},layers:baseLayers}
});
map.scrollZoom.disable();
map.addControl(new maplibregl.NavigationControl({showCompass:false}),'top-right');
map.addControl(new maplibregl.AttributionControl({compact:false}),'bottom-right');
const popup=new maplibregl.Popup({closeButton:false,closeOnClick:false,offset:10});
const colors=['interpolate',['linear'],['get','gm'],.20,'#f79837',.25,'#d5c3a8',.30,'#7ebacb',.35,'#299ac6',.40,'#0085ca'];
function boundsFor(fs,clip=false){
  const b=new maplibregl.LngLatBounds();
  function visit(a){if(typeof a[0]==='number'){if(!clip||(a[0]>=110&&a[0]<=156&&a[1]>=-44&&a[1]<=-8))b.extend(a);}else a.forEach(visit);}
  fs.forEach(f=>visit(f.geometry.coordinates));return b;
}
function fit(){if(features.length){map.fitBounds(boundsFor(features,true),{padding:{top:62,bottom:112,left:30,right:30},maxZoom:9,duration:600});}}
function closeDetail(){selected=null;$('detail').hidden=true;$('search').value='';if(map.getLayer('selected'))map.setFilter('selected',['==',['get','code'],'']);}
function showDetail(f,fly=false){
  const p=f.properties;if(!p.hasData)return;
  selected=p.code;$('name').textContent=p.LGA_NAME_2025;$('state').textContent=p.STATE_NAME_2021+' · '+p.code;
  $('gm').textContent=pct(p.gm);$('revenue').textContent=money(p.revenue);$('orders').textContent=number(p.orders);$('lines').textContent=number(p.lines);$('detail').hidden=false;$('search').value=p.code;
  map.setFilter('selected',['==',['get','code'],p.code]);
  if(fly)map.fitBounds(boundsFor([f]),{padding:70,maxZoom:10,duration:800});
}
function applyData(){
  features=[];
  for(const f of geography.features){const p=f.properties;const d=rows.get(p.LGA_CODE_2025);f.id=p.LGA_CODE_2025;f.properties={...p,code:p.LGA_CODE_2025,hasData:!!d,...(d||{gm:0,revenue:0,margin:0,lines:0,orders:0})};if(d)features.push(f);}
  map.getSource('lgas').setData(geography);
  const options=[new Option('Find an LGA…','')];
  [...features].sort((a,b)=>a.properties.LGA_NAME_2025.localeCompare(b.properties.LGA_NAME_2025)).forEach(f=>options.push(new Option(f.properties.LGA_NAME_2025+' · '+f.properties.STATE_NAME_2021,f.properties.code)));
  $('search').replaceChildren(...options);
  let revenue=0,margin=0;for(const d of rows.values()){revenue+=d.revenue;margin+=d.margin;}
  $('summary').textContent=features.length+' areas · '+money(revenue)+' revenue';
  if(!features.length){$('status').textContent='No LGA data for the current filters.';$('status').hidden=false;}else $('status').hidden=true;
  closeDetail();fit();
}
map.on('load',async()=>{
  try{
    const response=await fetch('australia-lga-2025.geojson');if(!response.ok)throw new Error('Boundary download failed');geography=await response.json();
    map.addSource('lgas',{type:'geojson',data:geography,promoteId:'LGA_CODE_2025',attribution:'<a href="https://www.abs.gov.au/statistics/standards/australian-statistical-geography-standard-asgs" target="_blank" rel="noopener">ABS LGA 2025</a>'});
    const before=map.getStyle().layers.find(l=>l.type==='symbol')?.id;
    map.addLayer({id:'lga-fill',type:'fill',source:'lgas',paint:{'fill-color':['case',['get','hasData'],colors,'#29404c'],'fill-opacity':['case',['get','hasData'],.78,.15]}},before);
    map.addLayer({id:'lga-outline',type:'line',source:'lgas',paint:{'line-color':'#aacbd9','line-opacity':.45,'line-width':['interpolate',['linear'],['zoom'],3,.3,7,.8,10,1.3]}},before);
    map.addLayer({id:'selected',type:'line',source:'lgas',filter:['==',['get','code'],''],paint:{'line-color':'#fff','line-width':2.7}});
    map.addLayer({id:'hovered',type:'line',source:'lgas',filter:['==',['get','code'],''],paint:{'line-color':'#e2f3ff','line-width':1.8}});
    ready=true;applyData();
  }catch(e){$('status').hidden=false;$('status').textContent='The map could not load. Refresh to retry.';console.error(e);}
});
map.on('mousemove','lga-fill',e=>{
  const f=e.features[0],p=f.properties;
  map.getCanvas().style.cursor=p.hasData?'pointer':'';
  if(!p.hasData){popup.remove();map.setFilter('hovered',['==',['get','code'],'']);return;}
  map.setFilter('hovered',['==',['get','code'],p.code]);
  const box=document.createElement('div'),title=document.createElement('b'),text=document.createElement('span');title.textContent=p.LGA_NAME_2025;text.textContent=pct(p.gm)+' margin · '+money(p.revenue);box.append(title,text);popup.setLngLat(e.lngLat).setDOMContent(box).addTo(map);
});
map.on('mouseleave','lga-fill',()=>{map.getCanvas().style.cursor='';popup.remove();map.setFilter('hovered',['==',['get','code'],'']);});
map.on('click','lga-fill',e=>{const code=e.features[0].properties.code;const f=features.find(f=>f.properties.code===code);if(f)showDetail(f);});
$('search').addEventListener('change',()=>{const f=features.find(f=>f.properties.code===$('search').value);if(f)showDetail(f,true);else closeDetail();});
$('close').addEventListener('click',closeDetail);
$('reset').addEventListener('click',()=>{closeDetail();fit();});
window.addEventListener('hashchange',()=>{readRows();if(ready)applyData();});
new ResizeObserver(()=>map.resize()).observe(document.body);
