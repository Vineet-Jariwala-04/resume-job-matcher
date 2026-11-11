(function(){
  // theme (same "theme" key as the rest of the app, so the choice carries over)
  var root=document.documentElement, toggle=document.getElementById("themeToggle");
  var saved=localStorage.getItem("theme")||"light";
  function apply(t){root.setAttribute("data-theme",t); if(toggle) toggle.checked=(t==="dark");}
  apply(saved);
  try{ if(!localStorage.getItem("theme")) localStorage.setItem("theme",saved); }catch(e){}
  if(toggle) toggle.addEventListener("change",function(){
    var t=toggle.checked?"dark":"light"; apply(t); localStorage.setItem("theme",t);
  });

  // show / hide password
  document.querySelectorAll(".pw-btn").forEach(function(b){
    b.addEventListener("click",function(){
      var i=document.getElementById(b.dataset.target), show=i.type==="password";
      i.type=show?"text":"password"; b.setAttribute("aria-label",show?"Hide password":"Show password");
      b.classList.toggle("on",show);
    });
  });

  // light client-side hints (server still validates everything)
  var pw=document.getElementById("password"), hint=document.getElementById("pwHint");
  if(pw && hint && pw.getAttribute("minlength")){
    pw.addEventListener("input",function(){
      var n=pw.value.length; if(!n){hint.textContent="At least 6 characters";hint.className="hint";return;}
      var ok=n>=6; hint.textContent=ok?"Looks good":"Need "+(6-n)+" more character"+(6-n>1?"s":"");
      hint.className="hint"+(ok?"":" bad");
    });
  }
  // prevent double submit
  document.querySelectorAll("form.auth-form").forEach(function(f){
    f.addEventListener("submit",function(){
      var b=f.querySelector(".btn"); if(b&&f.checkValidity()){b.disabled=true;b.textContent="Please wait…";}
    });
  });
})();
