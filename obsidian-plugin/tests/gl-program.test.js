'use strict';

// Asynchronous shader program creation against a mocked WebGL context.

const assert = require('node:assert/strict');
const { createProgramAsync } = require('../src/gl-program.js');

function mockGL({ parallel = true, pendingPolls = 0, linkOk = true } = {}) {
  let polls = 0;
  const gl = {
    VERTEX_SHADER: 1, FRAGMENT_SHADER: 2, COMPILE_STATUS: 3, LINK_STATUS: 4,
    shaders: [], programs: [], calls: [],
    getExtension(name) {
      this.calls.push(['getExtension', name]);
      return parallel ? { COMPLETION_STATUS_KHR: 5 } : null;
    },
    createShader(type) { const shader = { type, deleted: false }; this.shaders.push(shader); return shader; },
    shaderSource(shader, source) { shader.source = source; },
    compileShader(shader) { shader.compiled = true; },
    createProgram() { const program = { deleted: false }; this.programs.push(program); return program; },
    attachShader(program, shader) { (program.shaders ||= []).push(shader); },
    detachShader(program, shader) { this.calls.push(['detachShader', shader]); },
    bindAttribLocation(program, location, name) { this.calls.push(['bindAttribLocation', location, name]); },
    linkProgram(program) { program.linked = true; },
    getProgramParameter(program, name) {
      this.calls.push(['getProgramParameter', name]);
      if (name === 5) return polls++ >= pendingPolls;
      if (name === this.LINK_STATUS) return linkOk;
      throw new Error('unexpected program status enum ' + name);
    },
    getShaderParameter(shader, name) {
      this.calls.push(['getShaderParameter', name]);
      assert.equal(name, this.COMPILE_STATUS);
      return true;
    },
    getShaderInfoLog() { return ''; },
    getProgramInfoLog() { return 'mock link failure'; },
    deleteShader(shader) { shader.deleted = true; this.calls.push(['deleteShader', shader]); },
    deleteProgram(program) { program.deleted = true; this.calls.push(['deleteProgram', program]); },
    isContextLost() { return false; },
  };
  return gl;
}

const delay = (ms = 30) => new Promise(resolve => setTimeout(resolve, ms));

async function main() {
  {
    const gl = mockGL({ pendingPolls: 2 });
    const promise = createProgramAsync(gl, 'vertex', 'fragment', { aTS: 0 });
    await delay(12);
    assert.equal(gl.calls.some(c => c[0] === 'getShaderParameter'), false, 'compile status queried while parallel link pending');
    assert.equal(gl.calls.some(c => c[0] === 'getProgramParameter' && c[1] === gl.LINK_STATUS), false, 'link status queried while parallel link pending');
    const program = await promise;
    assert.equal(program, gl.programs[0]);
    assert.equal(program.deleted, false);
    assert.deepEqual(gl.shaders.map(s => s.deleted), [true, true]);
    assert(gl.calls.some(c => c[0] === 'bindAttribLocation' && c[1] === 0 && c[2] === 'aTS'));
  }
  {
    const gl = mockGL({ linkOk: false });
    await assert.rejects(createProgramAsync(gl, 'v', 'f'), /program link error: mock link failure/);
    assert.equal(gl.programs[0].deleted, true, 'failed link program was not deleted');
    assert.deepEqual(gl.shaders.map(s => s.deleted), [true, true]);
  }
  {
    const gl = mockGL({ pendingPolls: 5 });
    let cancelled = false;
    const promise = createProgramAsync(gl, 'v', 'f', null, () => cancelled);
    await delay(2);
    cancelled = true;
    await assert.rejects(promise, /program link cancelled/);
    assert.equal(gl.programs[0].deleted, true, 'cancelled program was not deleted');
    assert.deepEqual(gl.shaders.map(s => s.deleted), [true, true]);
  }
  {
    const gl = mockGL({ parallel: false });
    const program = await createProgramAsync(gl, 'v', 'f');
    assert.equal(program, gl.programs[0], 'non-extension fallback did not complete');
    assert.equal(gl.programs[0].deleted, false);
    assert.deepEqual(gl.shaders.map(s => s.deleted), [true, true]);
  }
  console.log('gl-program: PASS (parallel deferral, success cleanup, link failure cleanup, cancellation cleanup, no-extension fallback)');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
