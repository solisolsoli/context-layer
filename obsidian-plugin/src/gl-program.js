'use strict';

// Compiles and links a WebGL program without blocking the main thread on the
// result. Compile/link work is submitted immediately; readiness is checked
// from later timer tasks so drivers with KHR_parallel_shader_compile can do
// the heavy work in the background. `isCancelled` lets the owning view abort
// (and clean up) when it closes or loses its context mid-link.
function createProgramAsync(gl, vsSource, fsSource, attribBindings, isCancelled) {
  return new Promise((resolve, reject) => {
    let vertex = null, fragment = null, program = null, finished = false;
    const parallel = gl.getExtension('KHR_parallel_shader_compile');
    function removeShader(shader) {
      if (!shader) return;
      try { if (program && gl.detachShader) gl.detachShader(program, shader); } catch (_) { /* context may be gone */ }
      try { gl.deleteShader(shader); } catch (_) { /* context may be gone */ }
    }
    function cleanup(deleteProgram) {
      removeShader(vertex); removeShader(fragment);
      if (deleteProgram && program) { try { gl.deleteProgram(program); } catch (_) { /* context may be gone */ } }
    }
    function fail(error) {
      if (finished) return;
      finished = true; cleanup(true); reject(error instanceof Error ? error : new Error(String(error)));
    }
    function poll() {
      if (finished) return;
      try {
        if (isCancelled && isCancelled()) return fail(new Error('program link cancelled'));
        if (gl.isContextLost && gl.isContextLost()) return fail(new Error('WebGL context lost while linking program'));
        if (parallel && !gl.getProgramParameter(program, parallel.COMPLETION_STATUS_KHR)) {
          setTimeout(poll, 8); return;
        }
        // Status queries wait until the parallel compile reports completion.
        // Without the extension they still run in a later task.
        const vertexOk = gl.getShaderParameter(vertex, gl.COMPILE_STATUS);
        const fragmentOk = gl.getShaderParameter(fragment, gl.COMPILE_STATUS);
        if (!vertexOk || !fragmentOk) {
          const logs = [!vertexOk ? 'vertex: ' + gl.getShaderInfoLog(vertex) : '', !fragmentOk ? 'fragment: ' + gl.getShaderInfoLog(fragment) : ''].filter(Boolean).join('\n');
          return fail(new Error('shader compile error: ' + logs));
        }
        if (!gl.getProgramParameter(program, gl.LINK_STATUS)) return fail(new Error('program link error: ' + gl.getProgramInfoLog(program)));
        finished = true; cleanup(false); resolve(program);
      } catch (error) { fail(error); }
    }
    try {
      vertex = gl.createShader(gl.VERTEX_SHADER);
      fragment = gl.createShader(gl.FRAGMENT_SHADER);
      if (!vertex || !fragment) throw new Error('WebGL could not allocate shader objects');
      gl.shaderSource(vertex, vsSource); gl.compileShader(vertex);
      gl.shaderSource(fragment, fsSource); gl.compileShader(fragment);
      program = gl.createProgram();
      if (!program) throw new Error('WebGL could not allocate a program object');
      gl.attachShader(program, vertex); gl.attachShader(program, fragment);
      if (attribBindings) for (const name in attribBindings) gl.bindAttribLocation(program, attribBindings[name], name);
      gl.linkProgram(program);
      setTimeout(poll, 8);
    } catch (error) { fail(error); }
  });
}

module.exports = { createProgramAsync };
