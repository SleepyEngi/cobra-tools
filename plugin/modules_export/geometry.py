import logging
import struct

import mathutils

from generated.formats.ms2.compounds.MeshDataWrap import MeshDataWrap
from generated.formats.ms2.compounds.packing_utils import USHORT_MAX, remap
from generated.formats.ms2.enums.MeshFormat import MeshFormat
from plugin.utils.object import get_property
from plugin.modules_export.armature import handle_transforms
from plugin.modules_export.mesh_chunks import DYNAMIC_ID, ChunkedMesh, NO_BONES_ID, DISCARD_STATIC_TRIS
from plugin.utils.matrix_util import ensure_tri_modifier, evaluate_mesh
from plugin.utils.shell import num_fur_as_weights, is_fin, is_shell


def export_model(model_info, b_lod_coll, b_ob, b_me, bones_table, bounds, apply_transforms, use_stock_normals_tangents, m_lod, shell_index, shell_count):
	logging.info(f"Exporting mesh {b_me.name}")
	# we create a ms2 mesh
	wrapper = MeshDataWrap(model_info.context)
	mesh = wrapper.mesh
	# set data
	mesh.flag._value = get_property(b_me, "flag")
	mesh.unk_floats[:] = (get_property(b_me, "unk_f0"), get_property(b_me, "unk_f1"))

	# register this format for all vert chunks that will be created later
	if mesh.context.version >= 52:
		mesh.mesh_format = MeshFormat[b_me.cobra.mesh_format]
	mesh.update_dtype()
	num_uvs = mesh.get_uv_count()
	num_vcols = mesh.get_vcol_count()
	# ensure that these are initialized
	mesh.tri_indices = []
	mesh.verts = []
	model_info.model.meshes.append(wrapper)

	num_fur_weights = num_fur_as_weights(b_me.materials[0].name)

	if not len(b_me.vertices):
		raise AttributeError(f"Mesh {b_ob.name} has no vertices!")

	if not len(b_me.polygons):
		raise AttributeError(f"Mesh {b_ob.name} has no polygons!")

	for len_type, num_type, name_type in (
			(len(b_me.uv_layers), num_uvs - num_fur_weights, "UV Maps"),
			(len(b_me.vertex_colors), num_vcols, "Color Attributes")):
		logging.debug(f"{name_type} count: {num_type}")
		if len_type != num_type:
			raise AttributeError(f"Mesh {b_ob.name} has {len_type} {name_type}, but {num_type} were expected!")

	# make sure the mesh has a triangulation modifier
	ensure_tri_modifier(b_ob)
	eval_obj, eval_me = evaluate_mesh(b_ob)
	# validate the mesh to get rid of degenerate geometry such as duplicate faces, which would trigger chunking asserts
	eval_me.validate()
	handle_transforms(eval_obj, eval_me, apply=apply_transforms)
	bounds.append(scale_bbox(eval_obj, apply_transforms))

	hair_length = get_hair_length(b_ob)
	mesh.fur_length = hair_length

	# tangents have to be pre-calculated; this will also calculate loop normal
	try:
		eval_me.calc_tangents(uvmap="UV0")
	except RuntimeError:
		raise RuntimeError(f"Tangent space calculation for model {b_ob.name} failed. Make sure it has UV0 and valid geometry")
	# these were stored on import per loop
	if use_stock_normals_tangents:
		ct_tangents = eval_me.attributes["ct_tangents"]
		ct_normals = eval_me.attributes["ct_normals"]
	if "RGBA0" in eval_me.attributes:
		rgba0_layer = eval_me.attributes["RGBA0"]
	else:
		rgba0_layer = None

	unweighted_vertices = []

	shell_ob = None
	vcol = (0, 0, 0, 0)
	# shape key morphing

	def get_shapekey(vert_index):
		return 0, 0, 0

	# eval_me does not have shape_keys, not sure if there is a way to consistently get them with modifiers applied
	b_key = b_me.shape_keys
	if b_key and len(b_key.key_blocks) > 1:
		lod_key = b_key.key_blocks[1]
		# yes, there is a key object attached
		if lod_key.name.startswith("LOD"):
			# yes, we have a shapekey, so define how to get it
			def get_shapekey(vert_index):
				return lod_key.data[vert_index].co

	validate_vertex_groups(b_ob, bones_table)
	# calculate bone weights per vertex first to reuse data
	# vertex_bone_id, weights, fur_length, fur_width
	weights_data = [export_weights(b_ob, b_vert, bones_table, hair_length, unweighted_vertices, m_lod) for b_vert in
					eval_me.vertices]

	# report unweighted vertices
	should_have_no_weights = (hasattr(mesh.flag, "weights") and not mesh.flag.weights)
	if unweighted_vertices and bones_table:
		if should_have_no_weights:
			logging.info(f"Should have no weights and has none.")
		else:
			raise AttributeError(f"{b_ob.name} has {len(unweighted_vertices)} unweighted vertices!")

	# fin meshes have to grab tangents from shell
	if is_fin(b_ob):
		shell_obs = [ob for ob in b_lod_coll.objects if is_shell(ob) and ob is not b_ob]
		if shell_obs:
			shell_ob = shell_obs[0]
			logging.debug(f"Copying data for {b_ob.name} from base mesh {shell_ob.name}...")
			shell_eval_ob, shell_eval_me = evaluate_mesh(shell_ob)
			shell_eval_me.calc_tangents(uvmap="UV0")
			shell_kd = fill_kd_tree(shell_eval_me)
			fin_uv_layer = eval_me.uv_layers[0].data

	if mesh.context.version >= 52:
		t_map = {}
		# reuse existing chunks
		if use_stock_normals_tangents:
			# there is just 1 layer
			for b_face_map_layer in eval_me.face_maps:
				# one per face
				for face_i, m_face_map in enumerate(b_face_map_layer.data):
					if m_face_map.value not in t_map:
						t_map[m_face_map.value] = []
					t_map[m_face_map.value].append(eval_me.polygons[face_i])
			# ignore static chunks, just make them all dynamic
			t_list = [(-1, polys) for polys in t_map.values()]
		else:
			t_list = pre_chunk(bones_table, eval_me, t_map, weights_data)
	else:
		# no chunking by weights, just take all faces
		t_map = {-1: eval_me.polygons}
		t_list = list(t_map.items())

	# stores values retrieved from blender, will be packed into array later
	verts = []
	# list of tri lists to support chunks
	# always add to last entry
	tris_chunks = []
	for b_chunk_bone_id, b_chunk_faces in t_list:
		logging.debug(f"Exporting {len(b_chunk_faces)} tris for bone index {b_chunk_bone_id}")
		# use a dict mapping dummy vertices to their index for fast lookup
		# this is used to convert blender vertices (several UVs, normals per face corner) to ms2 vertices
		dummy_vertices = {}
		chunk_verts = []
		chunk_tris = []
		count_unique = 0
		count_reused = 0
		# loop faces and collect unique and repeated vertices
		for face in b_chunk_faces:
			if len(face.loop_indices) != 3:
				# this is a bug - we are applying the triangulation modifier above
				raise AttributeError(f"Mesh {b_ob.name} is not triangulated!")

			# build indices into vertex buffer for the current face and chunk
			tri = []
			# loop over face loop to get access to face corner data (normals, uvs, vcols, etc)
			for loop_index in face.loop_indices:
				b_loop = eval_me.loops[loop_index]
				b_vert = eval_me.vertices[b_loop.vertex_index]

				# get the vectors
				position = b_vert.co
				if shell_ob:
					lookup = fin_uv_layer[b_loop.index].uv.to_3d()
					lookup.z = b_vert.co.x
					co, index, dist = shell_kd.find(lookup)
					shell_loop = shell_eval_me.loops[index]
					normal = shell_loop.normal
					tangent = shell_loop.tangent
					negate_bitangent = False
				else:
					normal = b_loop.normal
					tangent = b_loop.tangent
					negate_bitangent = b_loop.bitangent_sign < 0.0
				# reindeer is a special case: has edited beard normals pointing straight down for shell & fins
				# if the custom normal is used, the tangent generated by blender does not appear to be correct
				# override with custom data if asked
				if use_stock_normals_tangents:
					normal = ct_normals.data[loop_index].vector
					tangent = ct_tangents.data[loop_index].vector
				# trees want to have custom normal and a vertex normal
				custom_normal = normal
				if b_me.cobra.mesh_format in ("INTERLEAVED_32", "INTERLEAVED_48") or mesh.flag == 517:
					normal = b_vert.normal

				shapekey = get_shapekey(b_loop.vertex_index)
				uvs = [(layer.data[loop_index].uv.x, 1 - layer.data[loop_index].uv.y) for layer in eval_me.uv_layers]
				# create a dummy bytes str for indexing
				float_items = [c for uv in uvs[:2] for c in uv] + [*normal]
				dummy = struct.pack(f'<IB{len(float_items)}f', b_loop.vertex_index, negate_bitangent, *float_items)

				# see if this dummy key exists
				try:
					# if it does - reuse it by grabbing its index from the dict
					v_index = dummy_vertices[dummy]
					count_reused += 1
				except KeyError:
					# it doesn't, so we have to fill in additional data
					v_index = count_unique
					if v_index > USHORT_MAX:
						raise OverflowError(
							f"{b_ob.name} has too many ms2 verts. The limit is {USHORT_MAX}. "
							f"\nBlender vertices have to be duplicated on every UV seam, hence the increase.")
					dummy_vertices[dummy] = v_index
					count_unique += 1

					# now collect any missing vert data that was not needed for the splitting of blender verts
					vertex_bone_id, weights, fur_length, fur_width, fur_clump = weights_data[b_loop.vertex_index]
					# use attribute api, ensure fallback so array setting does not choke
					if rgba0_layer:
						vcol = rgba0_layer.data[loop_index].color
						vcol[3] = 1.0 - fur_clump
					if num_fur_weights:
						# append to uv
						uvs.append((fur_length, remap(fur_width, 0, 1, -16, 16)))
					# store all raw blender data
					chunk_verts.append((
						position, vertex_bone_id == DYNAMIC_ID, normal, custom_normal, negate_bitangent,
						tangent, uvs, vcol, weights, shapekey))
				tri.append(v_index)
			# add it to the latest chunk, cast to tuple to make it hashable
			chunk_tris.append(tuple(tri))
		logging.debug(f"Preliminary chunk: unique {count_unique}, reused {count_reused} verts")
		# do final chunk splitting here, as we now have the final split vertices
		if mesh.context.version >= 52:
			# build table of neighbors
			cm = ChunkedMesh(chunk_tris, chunk_verts, tris_chunks, verts, b_chunk_bone_id, is_shell(b_ob))
			cm.partition()
		else:
			tris_chunks.append((b_chunk_bone_id, chunk_tris))
			verts.extend(chunk_verts)
	logging.debug(f"count_chunks {len(tris_chunks)}")

	len_b_tris = len(eval_me.polygons)
	sum_of_pre_chunk_tris = sum(len(tris) for i, tris in t_list)
	assert len_b_tris == sum_of_pre_chunk_tris, f"Lost {len_b_tris-sum_of_pre_chunk_tris} tris in 1st chunking"
	sum_of_final_chunk_tris = sum(len(tris) for i, tris in tris_chunks)
	assert len_b_tris == sum_of_final_chunk_tris, f"Lost {len_b_tris-sum_of_final_chunk_tris} tris in 2nd chunking"

	# update vert & tri array
	mesh.pack_base = model_info.pack_base
	# for JWE2 so we can store these on the tri chunks
	mesh.shell_index = shell_index
	mesh.shell_count = shell_count
	# transfer raw verts into mesh data packed array
	mesh.tris = tris_chunks
	try:
		mesh.set_verts(verts)
	except ValueError:
		raise AttributeError(f"Could not export {b_ob.name}!")
	return wrapper


def scale_bbox(b_ob, apply_transforms):
	if apply_transforms:
		# bound_box does not contain matrix_local transforms
		bbox = [b_ob.matrix_local @ mathutils.Vector(vec) for vec in b_ob.bound_box]
	else:
		bbox = b_ob.bound_box
	return bbox


def pre_chunk(bones_table, eval_me, t_map, weights_data):
	# preliminary chunking - by static weights
	# check which bones are used per face
	for face in eval_me.polygons:
		r = list(set(weights_data[v_index][0] for v_index in face.vertices))
		# are there weights at all?
		if not bones_table:
			face_vertex_bone_id = NO_BONES_ID
		# do all verts of this face use the same bone id?
		elif len(r) == 1:
			face_vertex_bone_id = r[0]
		else:
			face_vertex_bone_id = DYNAMIC_ID
		# append face for this bone id
		if face_vertex_bone_id not in t_map:
			t_map[face_vertex_bone_id] = list()
		t_map[face_vertex_bone_id].append(face)
	# deleting small static chunks only on dynamic meshes, static meshes will not have -1 in
	if DYNAMIC_ID in t_map:
		for face_vertex_bone_id, bone_tris in tuple(t_map.items()):
			# delete small static chunk
			if face_vertex_bone_id != DYNAMIC_ID and len(bone_tris) < DISCARD_STATIC_TRIS:
				logging.debug(f"Moving {len(bone_tris)} tris for bone {face_vertex_bone_id} to dynamic chunk")
				v_list = t_map.pop(face_vertex_bone_id)
				t_map[DYNAMIC_ID].extend(v_list)
	t_list = list(t_map.items())
	return t_list


def validate_vertex_groups(b_ob, bones_table):
	for v_group in b_ob.vertex_groups:
		if v_group.name in bones_table:
			continue
		elif v_group.name in ("fur_width", "fur_length"):
			continue
		else:
			logging.warning(f"Ignored extraneous vertex group {v_group.name} on mesh {b_ob.name}")


def export_weights(b_ob, b_vert, bones_table, hair_length, unweighted_vertices, m_lod):
	# defaults that may or may not be set later on
	# True if used, bone index if it isn't
	vertex_bone_id = DYNAMIC_ID
	fur_length = 0
	fur_width = 0
	fur_clump = 0
	# get the weights
	w = []
	for v_group in b_vert.groups:
		try:
			v_group_name = b_ob.vertex_groups[v_group.group].name
			if v_group_name == "fur_length":
				fur_length = v_group.weight * hair_length
			elif v_group_name == "fur_width":
				fur_width = v_group.weight
			elif v_group_name == "fur_clump":
				fur_clump = v_group.weight
			elif v_group_name in bones_table:
				# avoid dummy vertex groups without corresponding bones
				bone_index = bones_table[v_group_name]
				# update lod's bone cutoff
				m_lod.bone_index = max(m_lod.bone_index, bone_index + 1)
				if v_group.weight > 0.0:
					w.append([bone_index, v_group.weight])
		except:
			logging.exception(
				f"Vert with {len(b_vert.groups)} groups, index {v_group.group} into {len(b_ob.vertex_groups)} groups failed in {b_ob.name}")
	# get the strongest influences on this vert, truncate to 4
	weights_sorted = sorted(w, key=lambda x: x[1], reverse=True)[:4]
	# are there any weights at all
	if not weights_sorted:
		unweighted_vertices.append(b_vert.index)
	# this should no longer happen
	# is the strongest one actually weighted
	elif not weights_sorted[0][1] > 0.0:
		unweighted_vertices.append(b_vert.index)
	# more than one valid bone weight for this vertex?
	elif len(weights_sorted) == 1:
		vertex_bone_id = weights_sorted[0][0]
	return vertex_bone_id, weights_sorted, fur_length, fur_width, fur_clump


def get_hair_length(ob):
	if ob.particle_systems:
		psys = ob.particle_systems[0]
		return psys.settings.hair_length
	return 0


def fill_kd_tree(me):
	size = len(me.loops)
	kd = mathutils.kdtree.KDTree(size)
	uv_layer = me.uv_layers[0].data
	for i, loop in enumerate(me.loops):
		# include x coord in lookup to handle mirrored UVs
		lookup = uv_layer[loop.index].uv.to_3d()
		lookup.z = me.vertices[loop.vertex_index].co.x
		kd.insert(lookup, i)
	kd.balance()
	return kd
