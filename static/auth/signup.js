(function(){
  var form=document.getElementById("signupForm");
  if(!form || form.dataset.otp!=="1") return;

  var email=document.getElementById("email"), name=document.getElementById("name"),
      sendBtn=document.getElementById("sendOtpBtn"), otpBox=document.getElementById("otpBox"),
      code=document.getElementById("otpCode"), verifyBtn=document.getElementById("verifyOtpBtn"),
      msg=document.getElementById("otpMsg"), changeBtn=document.getElementById("changeEmailBtn"),
      submitBtn=document.getElementById("signupBtn"), note=document.getElementById("verifyNote");
  var verified=false, timer=null;

  function say(text,kind){ msg.textContent=text||""; msg.className="otp-msg"+(kind?" "+kind:""); }
  function post(url,data){
    return fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data)})
      .then(function(r){return r.json().catch(function(){return {ok:false,error:"Unexpected reply from the server."};});})
      .catch(function(){return {ok:false,error:"Could not reach the server. Is the app running?"};});
  }
  function countdown(seconds){
    clearInterval(timer);
    var left=seconds; sendBtn.disabled=true;
    (function tick(){
      if(left<=0){ clearInterval(timer); sendBtn.disabled=false; sendBtn.textContent="Resend OTP"; return; }
      sendBtn.textContent="Resend in "+left+"s"; left--;
    })();
    timer=setInterval(function(){
      if(left<=0){ clearInterval(timer); sendBtn.disabled=false; sendBtn.textContent="Resend OTP"; return; }
      sendBtn.textContent="Resend in "+left+"s"; left--;
    },1000);
  }
  function markVerified(){
    verified=true; clearInterval(timer);
    email.readOnly=true; otpBox.hidden=true;
    sendBtn.textContent="Verified \u2713"; sendBtn.disabled=true; sendBtn.classList.add("done");
    changeBtn.hidden=false; submitBtn.disabled=false; if(note) note.hidden=true;
    say("Email verified. You can finish signing up below.","ok");
  }
  function reset(){
    verified=false; clearInterval(timer);
    email.readOnly=false; otpBox.hidden=true; code.value="";
    sendBtn.textContent="Send OTP"; sendBtn.disabled=false; sendBtn.classList.remove("done");
    changeBtn.hidden=true; submitBtn.disabled=true; if(note) note.hidden=false; say("");
  }

  sendBtn.addEventListener("click",function(){
    if(!email.value || !email.checkValidity()){ say("Please enter a valid email address first.","bad"); email.focus(); return; }
    sendBtn.disabled=true; sendBtn.textContent="Sending\u2026"; say("Sending the code\u2026");
    post("/signup/send-otp",{email:email.value,name:name?name.value:""}).then(function(res){
      if(res.ok){ otpBox.hidden=false; code.value=""; code.focus(); say(res.message,"ok"); countdown(res.resend_in||60); }
      else { say(res.error||"Could not send the code.","bad"); if(res.resend_in){ countdown(res.resend_in); } else { sendBtn.disabled=false; sendBtn.textContent=otpBox.hidden?"Send OTP":"Resend OTP"; } }
    });
  });

  verifyBtn.addEventListener("click",function(){
    var digits=(code.value||"").replace(/\D/g,"");
    if(digits.length!==6){ say("Please enter the 6-digit code from your email.","bad"); code.focus(); return; }
    verifyBtn.disabled=true; say("Checking the code\u2026");
    post("/signup/verify-otp",{email:email.value,code:digits}).then(function(res){
      verifyBtn.disabled=false;
      if(res.ok){ markVerified(); } else { say(res.error||"Could not verify the code.","bad"); code.select(); }
    });
  });

  // Enter key: verify in the code box, send in the email box (never submit the form early)
  code.addEventListener("keydown",function(e){ if(e.key==="Enter"){ e.preventDefault(); verifyBtn.click(); } });
  email.addEventListener("keydown",function(e){ if(e.key==="Enter" && !verified){ e.preventDefault(); sendBtn.click(); } });
  // editing the email after asking for a code means the code no longer applies
  email.addEventListener("input",function(){ if(!verified && !otpBox.hidden){ otpBox.hidden=true; code.value=""; say(""); clearInterval(timer); sendBtn.disabled=false; sendBtn.textContent="Send OTP"; } });
  changeBtn.addEventListener("click",function(){ reset(); email.focus(); });

  if(form.dataset.verified==="1"){ markVerified(); }
})();
