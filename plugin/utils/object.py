import logging
import bpy


def mesh_from_data(scene, name, verts, faces, wireframe=False, coll_name=None, coll=None):
	me = bpy.data.meshes.new(name)
	me.from_pydata(verts, [], faces)
	# me.update()
	ob = create_ob(scene, name, me, coll_name=coll_name, coll=coll)
	if wireframe:
		ob.draw_type = 'WIRE'
	return ob, me


def create_ob(scene, ob_name, ob_data, coll_name=None, coll=None):
	logging.debug(f"Adding {ob_name} to scene {scene.name}")
	ob = bpy.data.objects.new(ob_name, ob_data)
	if coll_name is not None:
		link_to_collection(scene, ob, coll_name)
	elif coll is not None:
		coll.objects.link(ob)
	else:
		# link to scene root collection
		scene.collection.objects.link(ob)
	bpy.context.view_layer.objects.active = ob
	return ob


def get_lod(ob):
	for coll in bpy.data.collections:
		if "LOD" in coll.name and ob.name in coll.objects:
			return coll.name


def create_collection(scene, coll_name):
	# turn any relative collection names to include the scene prefix
	if not coll_name.startswith(f"{scene.name}_"):
		coll_name = f"{scene.name}_{coll_name}"
	if coll_name not in bpy.data.collections:
		coll = bpy.data.collections.new(coll_name)
		scene.collection.children.link(coll)
		return coll
	return bpy.data.collections[coll_name]


def link_to_collection(scene, ob, coll_name):
	# turn any relative collection names to include the scene prefix
	if not coll_name.startswith(f"{scene.name}_"):
		coll_name = f"{scene.name}_{coll_name}"
	if coll_name not in bpy.data.collections:
		coll = bpy.data.collections.new(coll_name)
		scene.collection.children.link(coll)
	else:
		coll = bpy.data.collections[coll_name]
	# Link active object to the new collection
	coll.objects.link(ob)
	return coll_name


class NedryError(Exception):
	"""For things users should not do"""

	def __init__(self, message="Ah ah ah, you didn't say the magic word!"):
		self.message = message
		super().__init__(self.message)

	def __str__(self):
		return f'{self.message}'


def has_objects_in_scene(scene):
	if scene.objects:
		# operator needs an active object, set one if missing (eg. user had deleted the active object)
		if not bpy.context.view_layer.objects.active:
			bpy.context.view_layer.objects.active = scene.objects[0]
		# now enter object mode on the active object, if we aren't already in it
		bpy.ops.object.mode_set(mode="OBJECT")
		return True


def get_property(ob, prop_name, default=None):
	"""Ensure that custom property is set or raise an intellegible error"""
	if prop_name in ob:
		return ob[prop_name]
	else:
		if default is not None:
			return default
		raise KeyError(f"Custom property '{prop_name}' missing from {ob.name} (data: {type(ob).__name__}). Add it!")


def set_collection_visibility(scene, coll_name, hide):
	# get view layer if it exists
	view_collections = bpy.context.view_layer.layer_collection.children
	if coll_name in view_collections:
		view_collections[coll_name].hide_viewport = hide
	scene_collections = scene.collection.children
	if coll_name in scene_collections:
		scene_collections[coll_name].hide_render = hide


def get_bones_table(b_armature_ob):
	p_bones = sorted(b_armature_ob.pose.bones, key=lambda pbone: pbone["index"])
	bones_table = [(bone["index"], bone.name) for bone in p_bones]
	return bones_table, p_bones


def get_p_index(pbone):
	if pbone:
		return pbone["index"]
	else:
		return None


def get_parent_map(p_bones):
	parent_index_map = [get_p_index(pbone.parent) for pbone in p_bones]
	return parent_index_map