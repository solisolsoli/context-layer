'use strict';

// Static checks of the GLSL ES 1.00 sources in src/shaders.js. No GLSL
// compiler runs here (none is installed; the fake WebGL context accepts any
// source), so this test catches what a compiler would reject most often:
// unbalanced delimiters, GLSL ES 3.00 syntax, reserved names, varyings that
// do not match between stages, unknown functions, and uniforms or
// attributes that view.js binds but the program does not declare (or binds
// with the wrong gl.uniform* type).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test, run } = require('./helpers/harness');
const Shaders = require('../src/shaders');

const viewSource = fs.readFileSync(path.join(__dirname, '..', 'src', 'view.js'), 'utf8');
const PROGRAMS = {
  point: { vs: Shaders.POINT_VS, fs: Shaders.POINT_FS, uniforms: 'pointUniforms', attribs: 'pointAttribs', short: 'pp' },
  line: { vs: Shaders.LINE_VS, fs: Shaders.LINE_FS, uniforms: 'lineUniforms', attribs: 'lineAttribs', short: 'lp' },
  quad: { vs: Shaders.QUAD_VS, fs: Shaders.QUAD_FS, uniforms: 'quadUniforms', attribs: 'quadAttribs', program: 'quadProgram' },
  composite: { vs: Shaders.QUAD_VS, fs: Shaders.COMPOSITE_FS, uniforms: 'compUniforms', attribs: 'compAttribs', program: 'compProgram' },
};
const BUILTINS = new Set(['clamp', 'dot', 'normalize', 'mix', 'smoothstep', 'length', 'exp', 'min', 'max', 'abs', 'step', 'sin', 'cos',
  'atan', 'sqrt', 'pow', 'fract', 'floor', 'texture2D', 'vec2', 'vec3', 'vec4', 'mat4', 'float', 'int', 'bool']);
const KEYWORDS = new Set(['if', 'else', 'for', 'while', 'return']);
const CALL_TYPE = { '1f': 'float', '2f': 'vec2', '3f': 'vec3', '4f': 'vec4', '1i': ['sampler2D', 'int'], 'Matrix4fv': 'mat4' };

const stripComments = src => src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');
function declarations(src, qualifier) {
  const out = new Map();
  for (const m of stripComments(src).matchAll(new RegExp('^\\s*' + qualifier + '\\s+(?:(?:lowp|mediump|highp)\\s+)?(\\w+)\\s+([^;]+);', 'gm'))) {
    for (const name of m[2].split(',').map(s => s.trim())) out.set(name, m[1]);
  }
  return out;
}
function functionsDefined(src) { return new Set(Array.from(stripComments(src).matchAll(/^\s*(?:void|float|vec[234]|mat4|int|bool)\s+(\w+)\s*\([^)]*\)\s*\{/gm), m => m[1])); }

for (const [name, p] of Object.entries(PROGRAMS)) {
  test(name + ' program: balanced delimiters, GLSL ES 1.00 only, a main in each stage', () => {
    for (const [stage, src] of [['vertex', p.vs], ['fragment', p.fs]]) {
      const code = stripComments(src);
      for (const [open, close] of [['{', '}'], ['(', ')'], ['[', ']']]) {
        let depth = 0;
        for (const ch of code) { if (ch === open) depth++; else if (ch === close) depth--; assert.ok(depth >= 0, `${stage}: ${close} before ${open}`); }
        assert.equal(depth, 0, `${stage}: unbalanced ${open}${close}`);
      }
      assert.doesNotMatch(code, /#version|^\s*(in|out)\s+\w+\s+\w+\s*;|\btexture\s*\(|\blayout\s*\(/m, `${stage}: GLSL ES 3.00 syntax`);
      assert.equal((code.match(/\bvoid\s+main\s*\(\s*\)/g) || []).length, 1, `${stage}: exactly one main()`);
      assert.match(code, stage === 'vertex' ? /\bgl_Position\s*=/ : /\bgl_FragColor\s*=/, `${stage}: writes its output`);
      const declared = ['uniform', 'attribute', 'varying'].flatMap(q => Array.from(declarations(src, q).keys()))
        .concat(Array.from(functionsDefined(src)))
        .concat(Array.from(code.matchAll(/\b(?:float|vec[234]|mat4|int|bool)\s+(\w+)\s*[=;,)]/g), m => m[1]));
      for (const id of declared) assert.ok(!id.startsWith('gl_') && !id.includes('__'), `${stage}: reserved identifier ${id}`);
      const known = new Set([...BUILTINS, ...functionsDefined(p.vs), ...functionsDefined(p.fs)]);
      for (const m of code.matchAll(/\b([A-Za-z_]\w*)\s*\(/g)) if (!KEYWORDS.has(m[1])) assert.ok(known.has(m[1]), `${stage}: unknown function ${m[1]}`);
    }
    assert.match(stripComments(p.fs), /precision\s+mediump\s+float\s*;/, 'fragment stage declares float precision');
  });

  test(name + ' program: varyings match between the stages', () => {
    const vs = declarations(p.vs, 'varying'), fs = declarations(p.fs, 'varying');
    for (const [v, type] of fs) {
      assert.ok(vs.has(v), `varying ${v} is read in the fragment stage but not written by the vertex stage`);
      assert.equal(vs.get(v), type, `varying ${v} has the same type in both stages`);
      assert.match(stripComments(p.vs), new RegExp('\\b' + v + '\\s*='), `varying ${v} is assigned`);
    }
  });

  test(name + ' program: every uniform and attribute view.js binds exists, with a matching gl.uniform* type', () => {
    const uniforms = new Map([...declarations(p.vs, 'uniform'), ...declarations(p.fs, 'uniform')]);
    const attributes = declarations(p.vs, 'attribute');
    const bound = new Map();
    if (p.short) {
      for (const m of viewSource.matchAll(new RegExp('(\\w+): uniform\\(' + p.short + ", '(\\w+)'\\)", 'g'))) bound.set(m[1], m[2]);
      const attribs = Array.from(viewSource.matchAll(new RegExp("attrib\\(" + p.short + ", '(\\w+)'\\)", 'g')), m => m[1]);
      assert.deepEqual(new Set(attribs), new Set(attributes.keys()), 'attributes looked up by view.js = attributes declared');
    } else {
      for (const m of viewSource.matchAll(new RegExp('(\\w+): gl\\.getUniformLocation\\(this\\.' + p.program + ", '(\\w+)'\\)", 'g'))) bound.set(m[1], m[2]);
      const attribs = Array.from(viewSource.matchAll(new RegExp('gl\\.getAttribLocation\\(this\\.' + p.program + ", '(\\w+)'\\)", 'g')), m => m[1]);
      assert.deepEqual(new Set(attribs), new Set(attributes.keys()));
    }
    assert.ok(bound.size > 0, 'view.js binds uniforms of this program');
    for (const [prop, uniform] of bound) assert.ok(uniforms.has(uniform), `${uniform} is bound but not declared`);
    for (const uniform of uniforms.keys()) assert.ok([...bound.values()].includes(uniform), `${uniform} is declared but never bound`);
    for (const m of viewSource.matchAll(new RegExp('gl\\.uniform(1f|2f|3f|4f|1i|Matrix4fv)\\(this\\.' + p.uniforms + '\\.(\\w+)', 'g'))) {
      const uniform = bound.get(m[2]), type = uniforms.get(uniform), expected = CALL_TYPE[m[1]];
      assert.ok(uniform, `uniform property ${m[2]} is bound`);
      assert.ok(Array.isArray(expected) ? expected.includes(type) : expected === type, `gl.uniform${m[1]} sets ${uniform}, declared ${type}`);
    }
  });
}

test('the shared breathing field is inlined into both point and ribbon vertex stages', () => {
  for (const vs of [Shaders.POINT_VS, Shaders.LINE_VS]) assert.match(vs, /vec3 neuralField\(vec3 p, float time\)/);
});

run('glsl');
