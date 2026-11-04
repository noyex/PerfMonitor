// The mercury drop behind the overview: WebGL on a fixed canvas, its surface chops with the stutter rate of the session.
// This app watches game performance, so the drop must stay cheap: at most 30 fps, nothing drawn while hidden or
// paused, one static frame with prefers-reduced-motion, a CSS drop without WebGL.
const Mercury = (() => {
  const FRAME_MS = 1000 / 30;
  const SCALE = 0.75;         // render size vs CSS pixels, the liquid is soft anyway
  const MIN_AMP = 0.12;       // a perfectly flat surface shows the plateau of the pillow as a grey disc
  const WRAP_S = 3600;        // keeps the noise inputs small in float32 when the window stays open for days
  const FRAGMENT = `
precision highp float;
uniform vec2 uRes; uniform float uT, uAmp, uFreq;
float hash(vec2 p){ return fract(sin(dot(p,vec2(41.3,289.1)))*43758.5453); }
float noise(vec2 p){ vec2 i=floor(p),f=fract(p),u=f*f*(3.-2.*f);
  return mix(mix(hash(i),hash(i+vec2(1,0)),u.x),mix(hash(i+vec2(0,1)),hash(i+vec2(1,1)),u.x),u.y); }
float smin(float a,float b,float k){ float h=clamp(.5+.5*(b-a)/k,0.,1.); return mix(b,a,h)-k*h*(1.-h); }
// Mercury drops as a 2D distance field, inflated into a pillow; the chop grows with the stutter rate of the session.
float sdf(vec2 p){
  float t=uT*.2;
  p+=.05*vec2(sin(p.y*2.6+t),cos(p.x*2.3-t*1.2));
  float d=length((p-vec2(.27+.02*sin(t),-.02+.02*cos(t*1.3)))*vec2(1.,1.08))-.37;
  d=smin(d,length(p-vec2(.6+.03*sin(t*.8),.3+.03*cos(t*.9)))-.1,.2);
  d=smin(d,length(p-vec2(.1+.03*cos(t*.7),-.42+.02*sin(t*1.1)))-.06,.16);
  return d;
}
float height(vec2 p){
  float d=sdf(p), k=clamp(-d/.3,0.,1.);
  float h=sqrt(max(0.,1.-(1.-k)*(1.-k)))*.3;
  float t=uT*.9;
  float q=uFreq*.42; vec2 w=p+.12*vec2(noise(p*4.+t*.3),noise(p*4.-t*.3));
  float chop=noise(w*q+t*.5)+.5*noise(w*q*2.1-t*.4)-.75;
  return h+chop*uAmp*.024*smoothstep(0.,.5,k);
}
vec3 env(vec3 d){
  float y=d.y;
  vec3 sky=mix(vec3(.9,.94,1.),vec3(.03,.035,.06),smoothstep(.02,.72,y));
  vec3 gnd=mix(vec3(.0),vec3(.28,.26,.36),smoothstep(-.1,-.85,y));
  vec3 c=y>0.?sky:gnd;
  c=mix(gnd,sky,smoothstep(-.012,.012,y));
  c+=vec3(1.)*smoothstep(.1,.0,abs(d.x-.45))*smoothstep(.2,.5,y)*smoothstep(.95,.6,y)*1.1;
  return c;
}
void main(){
  vec2 p=(gl_FragCoord.xy-.5*uRes)/uRes.y;
  float d=sdf(p);
  if(d>.01){ gl_FragColor=vec4(0,0,0,1); return; }
  float e=.0025, h=height(p);
  vec3 n=normalize(vec3(-(height(p+vec2(e,0))-h)/e,-(height(p+vec2(0,e))-h)/e,1.));
  vec3 r=reflect(vec3(0,0,-1),n);
  float fres=pow(1.-n.z,2.2);
  vec3 col=env(r);
  vec3 film=.5+.5*cos(6.283*(fres*.9+vec3(.0,.33,.62)));               // thin-film sheen at the rim
  col=mix(col,col*film*1.9,clamp(fres*.9,0.,.65));
  col*=.86;
  gl_FragColor=vec4(pow(col,vec3(.9))*smoothstep(.002,-.002,d),1.);
}`;

  const cv = document.getElementById("gl");
  const fallback = document.getElementById("gl-fallback");
  const calm = matchMedia("(prefers-reduced-motion: reduce)");
  let gl = null, uni = null;
  let shown = false, paused = false, raf = 0, next = 0, clock = 0;
  let amp = 0, ampTo = 0, freq = 22, settled = false;

  const compile = () => {
    const shader = (type, src) => {
      const o = gl.createShader(type);
      gl.shaderSource(o, src);
      gl.compileShader(o);
      return o;
    };
    const pr = gl.createProgram();
    gl.attachShader(pr, shader(gl.VERTEX_SHADER, "attribute vec2 a;void main(){gl_Position=vec4(a,0,1);}"));
    gl.attachShader(pr, shader(gl.FRAGMENT_SHADER, FRAGMENT));
    gl.bindAttribLocation(pr, 0, "a");
    gl.linkProgram(pr);
    if (!gl.getProgramParameter(pr, gl.LINK_STATUS)) return false;
    gl.useProgram(pr);
    gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    uni = Object.fromEntries(["uRes", "uT", "uAmp", "uFreq"].map((n) => [n, gl.getUniformLocation(pr, n)]));
    return true;
  };

  const draw = () => {
    if (!uni || gl.isContextLost()) return;
    gl.uniform2f(uni.uRes, cv.width, cv.height);
    gl.uniform1f(uni.uT, clock + 40);
    gl.uniform1f(uni.uAmp, amp);
    gl.uniform1f(uni.uFreq, freq);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  };

  const size = () => {
    if (!gl) return;
    cv.width = innerWidth * SCALE;
    cv.height = innerHeight * SCALE;
    gl.viewport(0, 0, cv.width, cv.height);
    if (shown) draw();   // resizing clears the canvas, also when no loop is running
  };

  const moving = () => shown && !paused && !calm.matches && !document.hidden && uni;

  const frame = (t) => {
    raf = 0;
    if (!moving()) return;
    raf = requestAnimationFrame(frame);
    if (t < next) return;
    next = t - next > 100 ? t + FRAME_MS : next + FRAME_MS;   // a late frame must not cost a burst of catch-up frames
    clock = (t / 1000) % WRAP_S;
    amp += (ampTo - amp) * 0.06;
    draw();
  };

  // Starts the loop when it should run, else settles on one frame with the final surface.
  const sync = () => {
    if (!shown || document.hidden) return;
    if (moving()) {
      if (!raf) raf = requestAnimationFrame(frame);
      return;
    }
    amp = ampTo;
    draw();
  };

  const start = () => {
    if (gl) return;
    gl = cv.getContext("webgl", { alpha: false, antialias: false, depth: false, stencil: false, powerPreference: "low-power" });
    if (gl && !compile()) gl = null;
    if (!gl) return;
    size();
    cv.addEventListener("webglcontextlost", (e) => {   // a GPU driver reset, the very thing this app records
      e.preventDefault();
      uni = null;
    });
    cv.addEventListener("webglcontextrestored", () => {
      compile();
      size();
      sync();
    });
  };

  addEventListener("resize", size);
  document.addEventListener("visibilitychange", sync);
  calm.addEventListener("change", sync);

  return {
    show: () => {
      shown = true;
      start();
      cv.hidden = !gl;
      fallback.hidden = !!gl;
      if (gl) size();
      sync();
    },
    hide: () => {
      shown = false;
      cv.hidden = fallback.hidden = true;
    },
    // A calmer session means calmer metal.
    surface: (perMin) => {
      ampTo = Math.max(MIN_AMP, Math.min(1, perMin / 26));
      freq = 14 + perMin * 1.1;
      if (!settled) amp = ampTo;   // the first session opens at rest, later ones ease into their surface
      settled = true;
      sync();
    },
    pause: (on) => {
      paused = on;
      sync();
    },
  };
})();
