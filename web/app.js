'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const keys = ['arena','subject_type','faction','era','rarity','category','role'];
  const rarityOrder = ['common','uncommon','rare','epic','legendary','mythic','celestial'];
  const model = {cards:[],rarities:{},filters:Object.fromEntries(keys.map(k=>[k,''])),search:'',sort:'catalog',view:'regular',limit:24,availableArt:null,now:Date.now(),activeTierWeight:0,tierCardWeights:{},laneCardWeights:{},tierFirst:false,releaseLanes:false,limitedShare:0};
  const esc = value => String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const clean = value => String(value ?? '').normalize('NFKD').replace(/\p{M}/gu,'').toLowerCase().replace(/ß/g,'ss').replace(/ς/g,'σ').replace(/[^\p{L}\p{N}]+/gu,' ').trim();
  const compact = value => clean(value).replace(/\s/g,'');
  const label = value => String(value || '').replace(/_/g,' ').replace(/\b\w/g,c=>c.toUpperCase());
  const num = value => Number.isFinite(Number(value)) ? Number(value) : 0;
  const fmt = value => new Intl.NumberFormat().format(num(value));
  const gatedCard = card => card.category==='legends'||card.availability==='event_only'||Boolean(card.event_id);
  const eventCard = card => gatedCard(card)||card.category==='limited'||Boolean(card.release);
  function collectorLimit(card){
    if(Number.isInteger(card.collector_limit)&&card.collector_limit>=1&&card.collector_limit<=6)return card.collector_limit;
    const limit=model.rarities[card.rarity]?.collector_limit;
    return Number.isInteger(limit)&&limit>=1&&limit<=6?limit:null;
  }
  const inView = card => model.view==='all'||(model.view==='events'?eventCard(card):!eventCard(card));
  const categoryLabel = category => category==='legends'?'Legends · Event exclusive':category==='canon'?'Canon / approved games':label(category);
  const collectionLabel = card => card.category==='canon'&&String(card.continuity||'').includes('user-approved Old Republic')?'Approved game continuity · Legends':card.category==='canon'?'Canon':categoryLabel(card.category);
  const percent = value => `${(num(value)*100).toLocaleString(undefined,{maximumFractionDigits:3,minimumFractionDigits:0})}%`;
  const icon = '<svg viewBox="0 0 44 44" aria-hidden="true"><path d="M7 11H5v28a3 3 0 0 0 3 3h22"/><rect x="10" y="3" width="27" height="36" rx="3"/><path d="m23.5 11 6 7-1 9-5 6-5-6-1-9Zm0 0v22m-6-15 6 4 6-4m-11 9 5-5 5 5"/></svg>';
  let toastTimer;
  function toast(text){$('toast').textContent=text;$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').hidden=true,3300);}
  function eligibility(card){
    const release=card.release;
    if(gatedCard(card))return {active:false,label:'Event exclusive · activation required'};
    if(card.category==='limited'&&!release)return {active:false,label:'Release not scheduled'};
    if(card.release_enabled===false||(model.releaseLanes&&card.category==='limited'&&card.release_enabled!==true))return {active:false,label:'Seasonal release disabled'};
    if(!release)return {active:true,label:'Standard pool'};
    const start=Date.parse(release.starts_at),end=Date.parse(release.ends_at);
    if(!Number.isFinite(start)||!Number.isFinite(end))return {active:false,label:'Release not scheduled'};
    if(model.now<start)return {active:false,label:'Upcoming release'};
    if(model.now>=end)return {active:false,label:'Release ended'};
    return {active:true,label:'Active release · supply dependent'};
  }
  function tierRate(rarity){
    const weight=num(model.rarities[rarity]?.spawn_weight);
    return model.tierFirst&&num(model.tierCardWeights[rarity])>0&&weight>0&&model.activeTierWeight>0?weight/model.activeTierWeight:null;
  }
  function rate(card){
    const tierChance=tierRate(card.rarity),weight=num(card.weight),total=num(model.tierCardWeights[card.rarity]);
    if(!eligibility(card).active||tierChance===null||weight<=0||total<=0)return null;
    if(!model.releaseLanes)return tierChance*weight/total;
    const limited=card.category==='limited',lane=limited?'limited':'regular',other=limited?'regular':'limited';
    const totals=model.laneCardWeights[card.rarity]||{},laneTotal=num(totals[lane]);
    if(laneTotal<=0)return null;
    const share=num(totals[other])>0?(limited?model.limitedShare:1-model.limitedShare):1;
    return tierChance*share*weight/laneTotal;
  }
  function oddsExplanation(card){
    if(card&&gatedCard(card))return 'No ordinary-pool rate. Access requires a separately enabled event and its release window.';
    const rule=model.releaseLanes?`Rarity is drawn first. When both groups are eligible, enabled limited editions share ${percent(model.limitedShare)} of that rarity and ordinary cards share ${percent(1-model.limitedShare)}; card weights apply within each group. An empty group gives its share to the other.`:'Rarity is drawn first, then card weights divide that tier.';
    return `${rule} These conditional odds use the published activation status and date, assuming every eligible card has supply. The archive cannot inspect private minted or reserved stock; sold-out cards and groups change live odds. Reload for updated activation and dates.`;
  }
  function rarityClass(card){return rarityOrder.includes(card.rarity)?`rarity-${card.rarity}`:'rarity-common';}
  function artHTML(card,detail=false){
    const path=String(card.card_image||'');
    const valid=/^\/cards\/[a-z0-9][a-z0-9_-]*\.png$/.test(path);
    const present=valid&&(model.availableArt===null||model.availableArt.has(card.id));
    const src=valid?path:'';
    return `<span class="card-art ${present?'loading':'unavailable'}">${present?`<img src="${esc(src)}" alt="Collectible card for ${esc(card.name)}" loading="${detail?'eager':'lazy'}" decoding="async">`:''}<span class="art-placeholder">${icon}<span>${present?'LOADING CARD IMAGE':'ART NOT INCLUDED'}</span></span></span>`;
  }
  function wireArt(root){root.querySelectorAll('.card-art img').forEach(img=>{const ready=()=>img.closest('.card-art').classList.add('ready');const fail=()=>{const parent=img.closest('.card-art');parent.classList.remove('loading');parent.classList.add('unavailable');parent.querySelector('.art-placeholder span').textContent='CARD IMAGE UNAVAILABLE';img.remove();};img.addEventListener('load',ready,{once:true});img.addEventListener('error',fail,{once:true});if(img.complete){if(img.naturalWidth)ready();else fail();}});}
  function cardHTML(card){
    return `<div class="card-item" role="listitem"><button type="button" class="card ${rarityClass(card)}" data-card="${esc(card.id)}" aria-label="Inspect ${esc(card.name)}, ${esc(label(card.rarity))}, ${esc(card.arena)}">${artHTML(card)}<span class="card-meta"><span class="card-name">${esc(card.name)}</span><span class="card-faction">${esc(card.faction)} · ${esc(label(card.role))}</span><span class="card-stats"><span><small>ATK</small>${fmt(card.attack)}</span><span><small>HP</small>${fmt(card.health)}</span><span><small>SPD</small>${fmt(card.speed)}</span></span><span class="card-rarity"><span>${esc(model.rarities[card.rarity]?.label||label(card.rarity))}</span><span>selection weight ${fmt(card.weight)}</span></span></span></button></div>`;
  }
  function matching(){
    const terms=clean(model.search).split(' ').filter(Boolean);
    const filtered=model.cards.filter(card=>inView(card)&&keys.every(k=>!model.filters[k]||String(card[k])===model.filters[k])&&terms.every(t=>card._search.includes(compact(t))));
    const getters={attack:c=>num(c.attack),health:c=>num(c.health),speed:c=>num(c.speed)};
    if(getters[model.sort])filtered.sort((a,b)=>getters[model.sort](b)-getters[model.sort](a)||a.name.localeCompare(b.name));
    else if(model.sort==='rarity')filtered.sort((a,b)=>rarityOrder.indexOf(b.rarity)-rarityOrder.indexOf(a.rarity)||num(a.weight)-num(b.weight)||a.name.localeCompare(b.name));
    else if(model.sort==='name')filtered.sort((a,b)=>a.name.localeCompare(b.name));
    return filtered;
  }
  function state(title,text,action){$('catalog-state').hidden=false;$('catalog-state').innerHTML=`<span class="state-symbol" aria-hidden="true">◇</span><h3>${esc(title)}</h3><p>${esc(text)}</p>${action?`<button type="button" id="state-action">${esc(action.label)}</button>`:''}`;if(action)$('state-action').addEventListener('click',action.run);}
  function render(){
    if(!model.cards.length)return;
    const cards=matching(),shown=cards.slice(0,model.limit);
    $('card-grid').innerHTML=shown.map(cardHTML).join('');
    $('result-count').textContent=cards.length===model.cards.length?`${shown.length} of ${cards.length} cards`:`${shown.length} of ${cards.length} matches · ${model.cards.length} in catalog`;
    document.querySelectorAll('[data-view]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.view===model.view)));
    $('catalog-state').hidden=cards.length>0;
    if(!cards.length)state('Nothing in this sector.','Try a different name, alias or combination of filters.',{label:'Reset the search',run:reset});
    $('load-more').hidden=shown.length>=cards.length;
    $('active-filters').innerHTML=keys.filter(k=>model.filters[k]).map(k=>`<button class="active-filter" type="button" data-clear="${k}">${esc(model.filters[k])}<span aria-hidden="true">×</span><span class="sr-only">Remove ${k} filter</span></button>`).join('');
    wireArt($('card-grid'));
  }
  function reset(){model.search='';model.filters=Object.fromEntries(keys.map(k=>[k,'']));model.limit=24;$('search').value='';keys.forEach(k=>$(`${k}-filter`).value='');render();}
  function dates(release,gated=false){if(!release)return gated?'Event release not scheduled.':'Standard release. No limited window.';const f=v=>{const date=new Date(v);return Number.isFinite(date.valueOf())?date.toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'}):'Not specified';};return `${f(release.starts_at)} → ${f(release.ends_at)} · dates in your local time${release.supply_cap!=null?` · lifetime cap ${fmt(release.supply_cap)} copies`:''}`;}
  function powerProfileHTML(card){
    const profile=card.power_profile;
    if(!profile||!Number.isInteger(profile.combat)||profile.combat<1||profile.combat>5)return '';
    const review=({curated_design:'Reviewed combat rating',default_design:'Provisional baseline',depiction_override:'Reviewed for this depiction'})[profile.basis]||'Review status not specified';
    return `<div class="detail-section"><h3>Combat power</h3><p><b>Combat rating ${profile.combat}/5</b></p><p>Fighting capability reflects skill, Force powers or other abilities, feats and the depicted phase.</p>${profile.rationale?`<p>${esc(profile.rationale)}</p>`:''}<p class="rate-explain">${review}. A lore-based game-design rating, not a measured canonical quantity.</p></div>`;
  }
  function details(id){
    const card=model.cards.find(c=>c.id===id);if(!card)return;
    const aliases=[...new Set([card.name,...(card.aliases||[])])];const chance=rate(card);const tierChance=tierRate(card.rarity);const collectors=collectorLimit(card);const abilityName=card.ability_name||label(card.ability);const abilityText=card.ability_description||'See the bot’s card info for the precise battle effect.';
    $('card-detail').innerHTML=`<div class="detail-layout ${rarityClass(card)}"><div class="detail-art">${artHTML(card,true)}</div><div class="detail-copy"><p class="eyebrow">${esc(label(card.rarity))} / ${esc(card.arena)} ARENA</p><h2 id="detail-name">${esc(card.name)}</h2><div class="detail-tags"><span>Faction: ${esc(card.faction)}</span><span>Era: ${esc(card.era)}</span><span>${esc(collectionLabel(card))}</span><span>${esc(label(card.role))}</span>${card.subject_type?`<span>${esc(label(card.subject_type))}</span>`:''}</div><p class="detail-description">${esc(card.description)}</p><div class="detail-stats"><div><span>ATTACK</span><b>${fmt(card.attack)}</b></div><div><span>HEALTH</span><b>${fmt(card.health)}</b></div><div><span>SPEED</span><b>${fmt(card.speed)}</b></div></div>${powerProfileHTML(card)}<div class="detail-section"><h3>${esc(abilityName)}</h3><p>${esc(abilityText)}</p></div><div class="detail-section"><h3>Continuity & references</h3><p>${esc(card.continuity||'Continuity not specified')}</p>${Array.isArray(card.references)&&card.references.length?`<ul class="reference-links">${card.references.map((reference,index)=>`<li><a href="${esc(reference.url)}" target="_blank" rel="noopener noreferrer">Reference ${index+1} · ${esc(reference.label)}<span class="sr-only"> (opens in a new tab)</span></a></li>`).join('')}</ul>`:''}</div><div class="detail-section"><h3>Accepted names</h3><div class="aliases">${aliases.map(a=>`<span>${esc(a)}</span>`).join('')}</div><p class="rate-explain">Unicode, punctuation and spacing normalize consistently. Distinct characters are never merged by fuzzy guessing.</p></div><div class="detail-section"><h3>Scarcity & release</h3>${collectors===null?'':`<p>Up to <b>${collectors}</b> distinct collector${collectors===1?'':'s'} per new encounter · one copy each.</p>`}<p>Selection weight <b>${fmt(card.weight)}</b> · ${esc(eligibility(card).label)}${chance===null?'':`<br>Conditional rarity chance <b>${percent(tierChance)}</b><br>Conditional card chance <b>${percent(chance)}</b>`}</p><p class="rate-explain">${esc(oddsExplanation(card))}</p><p class="rate-explain">${esc(dates(card.release,gatedCard(card)))}</p><p class="rate-explain">${card.set?`Set: ${esc(card.set)}. `:''}Rarity contribution: ${fmt(model.rarities[card.rarity]?.power_multiplier||1)}× attack and health. Combat capability and role also shape the final values. Both arenas use the same stat scale. Narrative prominence, screen time, popularity and physical size add no power.</p></div><div class="card-id-row"><label class="sr-only" for="detail-id">Public card ID</label><input id="detail-id" value="${esc(card.id)}" readonly><button type="button" id="copy-id">Copy ID</button></div></div></div>`;
    wireArt($('card-detail'));$('copy-id').addEventListener('click',()=>copy(card.id,$('detail-id')));$('card-dialog').showModal();
  }
  async function copy(text,input){try{if(!navigator.clipboard)throw new Error();await navigator.clipboard.writeText(text);toast('Copied to clipboard.');}catch{if(input){input.focus();input.select();toast('Select the ID and copy it manually.');}else toast(`Copy: ${text}`);}}
  async function load(){
    state('Opening the archive','Loading public card definitions.');$('card-grid').innerHTML='';$('load-more').hidden=true;
    try{
      const response=await fetch('../api/catalog',{cache:'no-store'});if(!response.ok)throw new Error(`Catalog returned ${response.status}`);const payload=await response.json();
      if(!Array.isArray(payload.cards)||!payload.cards.length)throw new Error('Catalog is empty');
      model.cards=payload.cards.filter(c=>c&&typeof c.id==='string'&&typeof c.name==='string').map(c=>({...c,_search:compact([c.name,c.id,c.faction,c.era,c.role,c.ability_name,...(c.aliases||[])].join(' '))}));
      if(!model.cards.length)throw new Error('No card definitions');
      model.rarities=payload.rarities||{};model.now=Date.parse(payload.public?.served_at)||Date.now();model.availableArt=Array.isArray(payload.public?.art_available)?new Set(payload.public.art_available):null;
      const boosts=rarityOrder.filter(key=>Number.isFinite(Number(model.rarities[key]?.power_multiplier))).map(key=>`${model.rarities[key].label||label(key)} ×${fmt(model.rarities[key].power_multiplier)}`);
      $('rarity-power').textContent=`${boosts.length?`Rarity boosts: ${boosts.join(', ')}. `:''}Combat capability and role also shape attack and health. Speed stays role-based. Narrative prominence, screen time, popularity and physical size add no power. Roles and abilities can change matchup outcomes.`;
      model.releaseLanes=payload.spawn_model?.schema_version===3&&payload.spawn_model?.formula==='tier-first-release-lanes-v3'&&payload.spawn_model?.limited_share_percent===25;
      model.tierFirst=model.releaseLanes||(payload.spawn_model?.schema_version===2&&payload.spawn_model?.formula==='tier-first-v2');
      model.limitedShare=model.releaseLanes?payload.spawn_model.limited_share_percent/100:0;
      model.tierCardWeights={};model.laneCardWeights={};
      model.cards.filter(c=>eligibility(c).active&&num(c.weight)>0).forEach(card=>{
        model.tierCardWeights[card.rarity]=(model.tierCardWeights[card.rarity]||0)+num(card.weight);
        const totals=model.laneCardWeights[card.rarity]||(model.laneCardWeights[card.rarity]={regular:0,limited:0});
        totals[card.category==='limited'?'limited':'regular']+=num(card.weight);
      });
      model.activeTierWeight=rarityOrder.filter(key=>num(model.tierCardWeights[key])>0).reduce((sum,key)=>sum+Math.max(0,num(model.rarities[key]?.spawn_weight)),0);
      const chances=rarityOrder.filter(key=>tierRate(key)!==null).map(key=>`${model.rarities[key].label||label(key)} ${percent(tierRate(key))}`);
      $('rarity-chances').textContent=chances.length?`Conditional rarity odds: ${chances.join(', ')}. ${oddsExplanation()}`:'Conditional rarity odds are not available until the tier-first catalog is ready.';
      $('rate-formula').textContent=model.releaseLanes?'rarity chance × group share × card weight ÷ eligible weight in that group':'rarity chance × card weight ÷ eligible weight in that rarity';
      const limits=rarityOrder.filter(key=>Number.isInteger(model.rarities[key]?.collector_limit)).map(key=>`${model.rarities[key].label||label(key)} ${model.rarities[key].collector_limit}`);
      $('collector-rules').textContent=`${limits.length?`Collectors per encounter: ${limits.join(', ')}. `:''}Limited editions follow their rarity. An event can publish its own limit; inspect the card for its current rule. Remaining edition stock may reduce a new encounter’s slots. Existing encounters keep the limit shown when they appeared.`;
      $('total-cards').textContent=fmt(model.cards.length);$('regular-subject-count').textContent=fmt(new Set(model.cards.filter(c=>!eventCard(c)).map(c=>c.subject_id||c.id)).size);$('release-count').textContent=fmt(model.cards.filter(eventCard).length);
      $('regular-count').textContent=fmt(model.cards.filter(c=>!eventCard(c)).length);$('event-count').textContent=fmt(model.cards.filter(eventCard).length);$('all-count').textContent=fmt(model.cards.length);
      keys.forEach(key=>{const select=$(`${key}-filter`),first=select.options[0].textContent;let values=[...new Set(model.cards.map(c=>String(c[key]||'')).filter(Boolean))];if(key==='rarity')values=rarityOrder.filter(x=>values.includes(x));else values.sort((a,b)=>a.localeCompare(b));select.innerHTML=`<option value="">${esc(first)}</option>`+values.map(v=>`<option value="${esc(v)}">${esc(key==='rarity'?(model.rarities[v]?.label||label(v)):key==='category'?categoryLabel(v):label(v))}</option>`).join('');select.value=model.filters[key];});render();
    }catch{model.cards=[];$('result-count').textContent='Catalog unavailable';state('The archive is being prepared.','The public catalog could not be loaded. No player data is requested. Try again once the catalog service is ready.',{label:'Retry',run:load});}
  }
  const commands=[['/vault help','Command guide and collection rules.'],['/vault catalog','Search public card definitions.'],['/vault collection','Browse your private card instances.'],['/vault daily','Claim your available daily reward.'],['/vault deck','Build a battle roster. Use clear:True to empty the selected arena’s deck; your cards stay owned.'],['/vault duel','Challenge a player in the same arena.'],['/vault raid','Join a cooperative boss encounter.']];
  $('guide-commands').innerHTML=commands.map(([command,text])=>`<div class="guide-command"><code>${esc(command)}</code><span>${esc(text)}</span><button type="button" data-copy="${esc(command)}" aria-label="Copy ${esc(command)}">⧉</button></div>`).join('');
  $('guide-commands').addEventListener('click',e=>{const b=e.target.closest('[data-copy]');if(b)copy(b.dataset.copy);});
  ['guide-button','footer-guide'].forEach(id=>$(id).addEventListener('click',()=>$('guide-dialog').showModal()));$('rate-button').addEventListener('click',()=>$('rate-dialog').showModal());
  document.querySelectorAll('dialog').forEach(dialog=>{dialog.querySelector('.dialog-close').addEventListener('click',()=>dialog.close());dialog.addEventListener('click',e=>{if(e.target!==dialog)return;const r=dialog.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)dialog.close();});});
  $('search').addEventListener('input',e=>{model.search=e.target.value;model.limit=24;render();});keys.forEach(k=>$(`${k}-filter`).addEventListener('change',e=>{model.filters[k]=e.target.value;model.limit=24;render();}));$('sort').addEventListener('change',e=>{model.sort=e.target.value;model.limit=24;render();});$('reset-filters').addEventListener('click',reset);$('load-more').addEventListener('click',()=>{model.limit+=24;render();});$('card-grid').addEventListener('click',e=>{const b=e.target.closest('[data-card]');if(b)details(b.dataset.card);});$('active-filters').addEventListener('click',e=>{const b=e.target.closest('[data-clear]');if(!b)return;model.filters[b.dataset.clear]='';$(`${b.dataset.clear}-filter`).value='';model.limit=24;render();});
  $('catalog-scopes').addEventListener('click',e=>{const button=e.target.closest('[data-view]');if(!button)return;model.view=button.dataset.view;model.limit=24;if(model.filters.category&&!model.cards.some(c=>inView(c)&&c.category===model.filters.category)){model.filters.category='';$('category-filter').value='';}render();});
  $('category-filter').addEventListener('change',()=>{if(['legends','limited'].includes(model.filters.category)&&model.view==='regular'){model.view='events';render();}});
  document.addEventListener('keydown',e=>{if(e.key==='/'&&!e.ctrlKey&&!e.metaKey&&!e.altKey&&!document.querySelector('dialog[open]')&&!['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName)){e.preventDefault();$('search').focus();}});
  const media=matchMedia('(max-width:850px)');$('filter-panel').open=!media.matches;media.addEventListener('change',e=>$('filter-panel').open=!e.matches);
  load();
})();
