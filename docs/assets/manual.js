(function(){
  // 目錄高亮目前章節
  var links=[].slice.call(document.querySelectorAll('.toc a')),map={};
  links.forEach(function(a){map[a.getAttribute('href').slice(1)]=a});
  if('IntersectionObserver' in window){
    var io=new IntersectionObserver(function(es){es.forEach(function(e){
      if(!e.isIntersecting)return;
      links.forEach(function(a){a.removeAttribute('aria-current')});
      var a=map[e.target.id];if(a)a.setAttribute('aria-current','true');
    })},{rootMargin:'-15% 0px -75% 0px'});
    document.querySelectorAll('main section[id]').forEach(function(s){io.observe(s)});
  }
  // 複製按鈕
  document.querySelectorAll('[data-copy]').forEach(function(b){
    b.addEventListener('click',function(){
      var done=function(t){var old=b.textContent;b.textContent=t;setTimeout(function(){b.textContent=old},1400)};
      if(navigator.clipboard&&navigator.clipboard.writeText){
        navigator.clipboard.writeText(b.dataset.copy).then(function(){done('已複製')},function(){done('請手動複製')});
      }else done('請手動複製');
    });
  });
  // 確認點勾選：只記在這台電腦的瀏覽器
  var KEY=document.title==='Gmail SMTP 設定教學'?'gmailSmtpGuideChecks':'manualChecks:'+location.pathname,
      boxes=[].slice.call(document.querySelectorAll('[data-check]')),saved={};
  try{saved=JSON.parse(localStorage.getItem(KEY)||'{}')||{}}catch(e){saved={}}
  var text=document.getElementById('progressText');
  if(!boxes.length||!text)return;
  function update(){
    var n=boxes.filter(function(b){return b.checked}).length;
    text.textContent='已完成 '+n+'／'+boxes.length+' 個確認點';
  }
  boxes.forEach(function(b){
    b.checked=!!saved[b.dataset.check];
    b.addEventListener('change',function(){
      saved[b.dataset.check]=b.checked;
      try{localStorage.setItem(KEY,JSON.stringify(saved))}catch(e){}
      update();
    });
  });
  document.getElementById('resetChecks').addEventListener('click',function(){
    boxes.forEach(function(b){b.checked=false});saved={};
    try{localStorage.removeItem(KEY)}catch(e){}
    update();
  });
  update();
})();
