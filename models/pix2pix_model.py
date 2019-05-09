from __future__ import division
import numpy as np
import torch
import os
from torch.autograd import Variable
from .base_model import BaseModel
from models import networks
import sys, traceback
import h5py
import os.path
from scipy.misc import imsave
from models import hourglass

import torchvision.utils as vutils
from torch.nn import init

VERSION = 4

# torch.manual_seed(1)
class HourglassVariant(torch.nn.Module):
	def __init__(self, num_input, model):
		super(HourglassVariant, self).__init__()
		layer_list = list(model.children())
		removed = layer_list[1:-1]
		self.pred_layer = layer_list[-1]
		model= torch.nn.Sequential(*removed)

		uncertainty_layer = [torch.nn.Conv2d(64,1,(3, 3),(1, 1),(1, 1)), torch.nn.Sigmoid()]
		self.uncertainty_layer = torch.nn.Sequential(*uncertainty_layer)
		new_input_layer = torch.nn.Conv2d(num_input, 128,(7, 7),(1, 1),(3, 3))

		init.normal_(new_input_layer.weight.data, 0.0, 0.02)
		init.constant_(new_input_layer.bias.data, 0.0)


		self.new_model = torch.nn.Sequential(new_input_layer, model)

	def forward(self, input_):
		pred_feature = self.new_model(input_)

		pred_d = self.pred_layer(pred_feature)
		pred_confidence = self.uncertainty_layer(pred_feature)

		return pred_d, pred_confidence

class Pix2PixModel(BaseModel):
	def name(self):
		return 'Pix2PixModel'

	def __init__(self, opt, _isTrain):
		BaseModel.initialize(self, opt)

		self.mode = opt.mode
		self.num_input = opt.input_nc

		if self.mode == 'Ours_Bilinear':
			print("======================================  DIW NETWORK TRAIN FROM %s======================="%self.mode)

			new_model = hourglass.HourglassModel(self.num_input)

			print('===================Loading Pretrained Model OURS ===================================')

			if not _isTrain:
				if self.num_input == 7:
					model_parameters = self.load_network(new_model, 'G', 'best_depth_Ours_Bilinear_inc_7')
				elif self.num_input == 3:
					model_parameters = self.load_network(new_model, 'G', 'best_depth_Ours_Bilinear_inc_3')
				elif self.num_input == 6:
					model_parameters = self.load_network(new_model, 'G', 'best_depth_Ours_Bilinear_inc_6')
				else:
					print('Something Wrong')
					sys.exit()

				new_model.load_state_dict(model_parameters)

			new_model = torch.nn.parallel.DataParallel(new_model.cuda(), device_ids = [0,1,2,3])

			self.netG = new_model

		else:
			print('ONLY SUPPORT Ours_Bilinear')
			sys.exit()

		self.old_lr = opt.lr
		self.netG.train()

		if True:      
			self.criterion_joint = networks.JointLoss(opt) 
			# initialize optimizers
			self.optimizer_G = torch.optim.Adam(self.netG.parameters(),
												lr=opt.lr, betas=(0.9, 0.999))
			self.scheduler = networks.get_scheduler(self.optimizer_G, opt)
			print('---------- Networks initialized -------------')
			networks.print_network(self.netG)
			print('-----------------------------------------------')

	def set_writer(self, writer):
		self.writer = writer

	def set_input(self, stack_imgs, targets):
		self.input = stack_imgs
		self.targets = targets

	def forward(self):

		# run first network
		self.input_images = Variable(self.input.cuda(), requires_grad = False)
		human_mask = 1.0 - Variable(self.targets['env_mask'].cuda(), requires_grad=False).unsqueeze(1)
		keypoints_img = Variable(self.targets['keypoints_img'].cuda(), requires_grad=False).unsqueeze(1)

		# stack inputs
		stack_inputs = None

		if self.num_input == 7:
			input_log_depth = Variable(self.targets['input_log_depth'].cuda(), requires_grad=False).unsqueeze(1)
			input_confidence = Variable(self.targets['input_confidence'].cuda(), requires_grad=False).unsqueeze(1)
			stack_inputs = torch.cat((self.input_images, human_mask, keypoints_img, input_log_depth, input_confidence), 1)
		elif self.num_input == 6:
			input_log_depth = Variable(self.targets['input_log_depth'].cuda(), requires_grad=False).unsqueeze(1)
			input_confidence = Variable(self.targets['input_confidence'].cuda(), requires_grad=False).unsqueeze(1)
			stack_inputs = torch.cat((self.input_images, human_mask, input_log_depth, input_confidence), 1)
		elif self.num_input == 3:
			stack_inputs = self.input_images
		else:
			print('SOMETHING WRONG with num_input !!!!!!!!!!!!!!!!!!!!!!!')
			sys.exit()

		self.prediction_d, self.pred_confidence = self.netG.forward(stack_inputs)
		self.prediction_d = self.prediction_d.squeeze(1)
		self.pred_confidence = self.pred_confidence.squeeze(1)

	def get_image_paths(self):
		return self.image_paths


	def write_summary(self, mode_name, input_images, prediction_d, 
						pred_confidence, targets, n_iter, loss=None):

		invere_depth_pred = torch.exp(- prediction_d.data.cpu()).unsqueeze(1).repeat(1,3,1,1)

		invere_depth_gt = 1.0/targets['depth_gt'].unsqueeze(1).repeat(1,3,1,1)
		gt_mask = targets['gt_mask'].unsqueeze(1).repeat(1,3,1,1)

		invere_depth_gt = invere_depth_gt * gt_mask
		min_depth, max_depth = np.percentile(invere_depth_pred.numpy(), [1, 99])
		invere_depth_pred[invere_depth_pred > max_depth] = 0.0
		invere_depth_pred[invere_depth_pred < min_depth] = 0.0

		inv_depth_mask = invere_depth_pred * gt_mask

		human_mask = 1.0 - targets['env_mask'].unsqueeze(1).repeat(1,3,1,1)
		input_confidence = targets['input_confidence'].unsqueeze(1).repeat(1,3,1,1)
		pred_confidence_saved = pred_confidence.data.unsqueeze(1).repeat(1,3,1,1)

		if loss:
			self.writer.add_scalar(mode_name + '/loss', loss, n_iter)

		self.writer.add_image(mode_name + '/image', vutils.make_grid(input_images[:8,:,:,:].data.cpu(), normalize=True), n_iter)
		self.writer.add_image(mode_name + '/pred_full', vutils.make_grid(invere_depth_pred[:8,:,:,:], normalize=True), n_iter)
		self.writer.add_image(mode_name + '/pred_mask', vutils.make_grid(inv_depth_mask[:8,:,:,:], normalize=True), n_iter)
		self.writer.add_image(mode_name + '/pred_confidence', vutils.make_grid(pred_confidence_saved[:8,:,:,:], normalize=True), n_iter)

		self.writer.add_image(mode_name + '/gt_depth', vutils.make_grid(invere_depth_gt[:8,:,:,:], normalize=True), n_iter)
		self.writer.add_image(mode_name + '/gt_mask', vutils.make_grid(gt_mask[:8,:,:,:], normalize=True), n_iter)

		self.writer.add_image(mode_name + '/human_mask', vutils.make_grid(human_mask[:8,:,:,:], normalize=True), n_iter)
		self.writer.add_image(mode_name + '/input_confidence', vutils.make_grid(input_confidence[:8,:,:,:], normalize=True), n_iter)

	def backward_G(self, n_iter):
		# Combined loss
		self.loss_joint = self.criterion_joint(self.input_images, self.prediction_d, 
												self.pred_confidence,self.targets)
		print("Train loss is %f "%self.loss_joint)

		# add to tensorboard
		if n_iter % 100 == 0:
			self.write_summary('Train', self.input_images, self.prediction_d, 
								self.pred_confidence, self.targets, n_iter, self.loss_joint)

		self.loss_joint_var = self.criterion_joint.get_loss_var()
		self.loss_joint_var.backward()

	def optimize_parameters(self, n_iter):
		self.forward()
		self.optimizer_G.zero_grad()
		self.backward_G(n_iter)
		self.optimizer_G.step()


	def evlaute_M_error(self, input_, targets, n_iter, write_to_summary):
		# switch to evaluation mode
		input_imgs = Variable(input_.cuda() , requires_grad = False)
		# stack inputs
		human_mask = 1.0 - Variable(targets['env_mask'].cuda(), requires_grad=False).unsqueeze(1)
		keypoints_img = Variable(targets['keypoints_img'].cuda(), requires_grad=False).unsqueeze(1)

		if self.num_input == 7:
			input_log_depth = Variable(targets['input_log_depth'].cuda(), requires_grad=False).unsqueeze(1)
			input_confidence = Variable(targets['input_confidence'].cuda(), requires_grad=False).unsqueeze(1)
			stack_inputs = torch.cat((input_imgs, human_mask, keypoints_img, input_log_depth, input_confidence), 1)
		elif self.num_input == 6:
			input_log_depth = Variable(targets['input_log_depth'].cuda(), requires_grad=False).unsqueeze(1)
			input_confidence = Variable(targets['input_confidence'].cuda(), requires_grad=False).unsqueeze(1)
			stack_inputs = torch.cat((input_imgs, human_mask, input_log_depth, input_confidence), 1)
		elif self.num_input == 3:
			stack_inputs = input_imgs
		else:
			print('SOMETHING WRONG!!!!!!!!!!!!!!!!!!!!!!!')
			sys.exit()

		prediction_d, _ = self.netG.forward(stack_inputs)

		pred_log_d = prediction_d.squeeze(1)
		pred_d = torch.exp(pred_log_d)

		sc_inv_full, sc_inv_human, sc_inv_env, sc_inv_intra, sc_inv_inter = self.criterion_joint.compute_si_rmse(pred_log_d.data, targets)
		sc_inv_errors = [sc_inv_full.item(), sc_inv_human.item(), sc_inv_env.item(), sc_inv_intra.item(), sc_inv_inter.item()]

		return sc_inv_errors


	def eval_save_tum_img(self, input_, targets, save_path):
		input_imgs = Variable(input_.cuda() )
		human_mask = 1.0 - Variable(targets['env_mask'].cuda(), requires_grad=False).unsqueeze(1)
		keypoints_img = Variable(targets['keypoints_img'].cuda(), requires_grad=False).unsqueeze(1)

		if self.num_input == 7:
			input_log_depth = Variable(targets['input_log_depth'].cuda(), requires_grad=False).unsqueeze(1)
			input_confidence = Variable(targets['input_confidence'].cuda(), requires_grad=False).unsqueeze(1)
			stack_inputs = torch.cat((input_imgs, human_mask, keypoints_img, input_log_depth, input_confidence), 1)
		elif self.num_input == 6:
			input_log_depth = Variable(targets['input_log_depth'].cuda(), requires_grad=False).unsqueeze(1)
			input_confidence = Variable(targets['input_confidence'].cuda(), requires_grad=False).unsqueeze(1)
			stack_inputs = torch.cat((input_imgs, human_mask, input_log_depth, input_confidence), 1)
		elif self.num_input == 3:
			stack_inputs = input_imgs
		else:
			print('SOMETHING WRONG!!!!!!!!!!!!!!!!!!!!!!!')
			sys.exit()

		prediction_d, _ = self.netG.forward(stack_inputs) 
		prediction_d = torch.exp(prediction_d.squeeze(1))
		pred_confidence = pred_confidence.squeeze(1)

		if os.path.exists(save_path) == False:
			os.makedirs(save_path)

		for i in range(0, len(targets["img_1_path"])):

			youtube_dir = save_path + targets["img_1_path"][i].split('/')[-2]

			if os.path.exists(youtube_dir) == False:
				os.makedirs(youtube_dir)

			pred_d_ref = prediction_d.data[i,:,:].cpu().numpy()
			pred_confidence_ref = pred_confidence.data[i,:,:].cpu().numpy()
			saved_img = np.transpose(input_imgs[i,:,:,:].cpu().numpy(), (1,2,0))

			output_path = youtube_dir + '/' + targets["img_1_path"][i].split('/')[-1] 
			
			print('output_path', output_path)
			input_confidence = targets['input_confidence'][i]
			gt_depth = targets['depth_gt'][i]
			gt_mask = targets['gt_mask'][i]
			human_mask = 1.0 - targets['env_mask'][i]

			# K = targets['K'][i]
			# T_1_G = targets['T_1_G'][i]

			hdf5_file_write = h5py.File(output_path,'w')
			hdf5_file_write.create_dataset("/prediction/img", data=saved_img, dtype='float32')            
			hdf5_file_write.create_dataset("/prediction/pred_depth", data=pred_d_ref, dtype='float32')
			hdf5_file_write.create_dataset("/prediction/gt_depth", data=gt_depth, dtype='float32')
			hdf5_file_write.create_dataset("/prediction/gt_mask", data=gt_mask, dtype='float32')    
			hdf5_file_write.create_dataset("/prediction/input_confidence", data=input_confidence, dtype='float32')
			hdf5_file_write.create_dataset("/prediction/human_mask", data=human_mask, dtype='float32')


	def evaluate_tum_error(self, input_, targets, n_iter, write_to_summary):
		input_imgs = Variable(input_.cuda() , requires_grad = False)
		human_mask = 1.0 - Variable(targets['env_mask'].cuda(), requires_grad=False).unsqueeze(1)
		keypoints_img = Variable(targets['keypoints_img'].cuda(), requires_grad=False).unsqueeze(1)

		if self.num_input == 7:
			input_log_depth = Variable(targets['input_log_depth'].cuda(), requires_grad=False).unsqueeze(1)
			input_confidence = Variable(targets['input_confidence'].cuda(), requires_grad=False).unsqueeze(1)
			stack_inputs = torch.cat((input_imgs, human_mask, keypoints_img, input_log_depth, input_confidence), 1)
		elif self.num_input == 6:
			input_log_depth = Variable(targets['input_log_depth'].cuda(), requires_grad=False).unsqueeze(1)
			input_confidence = Variable(targets['input_confidence'].cuda(), requires_grad=False).unsqueeze(1)
			stack_inputs = torch.cat((input_imgs, human_mask, input_log_depth, input_confidence), 1)
		elif self.num_input == 3:
			stack_inputs = input_imgs
		else:
			print('SOMETHING WRONG!!!!!!!!!!!!!!!!!!!!!!!')
			sys.exit()

		prediction_d, _ = self.netG.forward(stack_inputs)
		# pred_confidence = pred_confidence.squeeze(1)
		# print('self.mode ', self.mode)

		pred_log_d = prediction_d.squeeze(1)
		pred_d = torch.exp(pred_log_d)

		sc_inv_full, sc_inv_human, sc_inv_env, sc_inv_intra, sc_inv_inter = self.criterion_joint.compute_si_rmse(pred_log_d.data, targets)
		l1_rel_full = self.criterion_joint.compute_l1_rel_error(pred_d.data, targets)
		RMSE_full = self.criterion_joint.compute_rmse_error(pred_d.data, targets)

		sc_inv_errors = [sc_inv_full.item(), sc_inv_human.item(), sc_inv_env.item(), sc_inv_intra.item(), sc_inv_inter.item()]

		return sc_inv_errors, l1_rel_full, RMSE_full	




	def eval_save_img(self, input_, targets, save_path):
		input_imgs = Variable(input_.cuda() , requires_grad = False)
		human_mask = 1.0 - Variable(targets['env_mask'].cuda(), requires_grad=False).unsqueeze(1)
		keypoints_img = Variable(targets['keypoints_img'].cuda(), requires_grad=False).unsqueeze(1)

		if self.num_input == 7:
			input_log_depth = Variable(targets['input_log_depth'].cuda(), requires_grad=False).unsqueeze(1)
			input_confidence = Variable(targets['input_confidence'].cuda(), requires_grad=False).unsqueeze(1)
			stack_inputs = torch.cat((input_imgs, human_mask, keypoints_img, input_log_depth, input_confidence), 1)
		elif self.num_input == 6:
			human_mask = 1.0 - Variable(targets['env_mask'].cuda(), requires_grad=False).unsqueeze(1)
			input_log_depth = Variable(targets['input_log_depth'].cuda(), requires_grad=False).unsqueeze(1)
			input_confidence = Variable(targets['input_confidence'].cuda(), requires_grad=False).unsqueeze(1)
			stack_inputs = torch.cat((input_imgs, human_mask, input_log_depth, input_confidence), 1)
		elif self.num_input == 3:
			stack_inputs = input_imgs
		else:
			print('SOMETHING WRONG!!!!!!!!!!!!!!!!!!!!!!!')
			sys.exit()

		prediction_log_d, _ = self.netG.forward(stack_inputs) 
		prediction_d = torch.exp(prediction_log_d.squeeze(1))

		if os.path.exists(save_path) == False:
			os.makedirs(save_path)

		for i in range(0, len(targets["img_1_path"])):

			youtube_dir = save_path + targets["img_1_path"][i].split('/')[-2]

			print('youtube_dir ', youtube_dir)

			if os.path.exists(youtube_dir) == False:
				os.makedirs(youtube_dir)

			pred_d_ref = prediction_d.data[i,:,:].cpu().numpy()
			saved_img = np.transpose(input_imgs[i,:,:,:].cpu().numpy(), (1,2,0))
			human_mask_ref = human_mask.data[i, 0, :, :].cpu().numpy()

			output_path = youtube_dir + '/' + targets["img_1_path"][i].split('/')[-1] 
			gt_depth_ref = targets['depth_gt'][i]
			gt_mask_ref = targets['gt_mask'][i]
			input_confidence_ref = targets['input_confidence'][i]
			keypoints_img = targets['keypoints_img'][i]

			input_depth_final_ref = targets['input_depth_final'][i]
			full_flow = targets['full_flow'][i]
			img_2 = targets['img_2'][i]

			K = targets['K'][i]
			T_1_G = targets['T_1_G'][i]

			hdf5_file_write = h5py.File(output_path,'w')
			hdf5_file_write.create_dataset("/prediction/img", data = saved_img)            
			hdf5_file_write.create_dataset("/prediction/pred_depth", data = pred_d_ref)

			hdf5_file_write.create_dataset("/prediction/gt_depth", data = gt_depth_ref)
			hdf5_file_write.create_dataset("/prediction/gt_mask", data = gt_mask_ref)    
			hdf5_file_write.create_dataset("/prediction/human_mask", data=human_mask_ref)
			hdf5_file_write.create_dataset("/prediction/input_confidence", data=input_confidence_ref)
			hdf5_file_write.create_dataset("/prediction/input_pp_depth", data=input_depth_final_ref)
			hdf5_file_write.create_dataset("/prediction/keypoints_img", data = keypoints_img)    
			hdf5_file_write.create_dataset("/prediction/full_flow", data = full_flow)    
			hdf5_file_write.create_dataset("/prediction/img_2", data = img_2)    

			hdf5_file_write.create_dataset("/prediction/K", data = K)    
			hdf5_file_write.create_dataset("/prediction/T_1_G", data = T_1_G)    
			hdf5_file_write.close()

	def run_and_save_videos_prediction(self, input_, targets, save_path):
		input_imgs = Variable(input_.cuda() , requires_grad = False)

		human_mask = 1.0 - Variable(targets['env_mask'].cuda(), requires_grad=False).unsqueeze(1)
		input_log_depth = Variable(targets['input_log_depth'].cuda(), requires_grad=False).unsqueeze(1)
		input_confidence = Variable(targets['input_confidence'].cuda(), requires_grad=False).unsqueeze(1)
		keypoints_img = Variable(targets['keypoints_img'].cuda(), requires_grad=False).unsqueeze(1)

		mvs_depth = Variable(targets['mvs_depth'].cuda(), requires_grad=False)
		input_depth = Variable(targets['input_depth'].cuda(), requires_grad=False)

		full_flow = targets['full_flow']

		if self.num_input == 7:
			stack_inputs = torch.cat((input_imgs, human_mask, keypoints_img, input_log_depth, input_confidence), 1)
		elif self.num_input == 6:
			stack_inputs = torch.cat((input_imgs, human_mask, input_log_depth, input_confidence), 1)
		elif self.num_input == 3:
			stack_inputs = input_imgs
		else:
			print('SOMETHING WRONG!!!!!!!!!!!!!!!!!!!!!!!')
			sys.exit()

		prediction_d, pred_confidence = self.netG.forward(stack_inputs) 

		pred_log_d = prediction_d.squeeze(1)
		pred_d = torch.exp(pred_log_d)
		pred_confidence = pred_confidence.squeeze(1)

		if os.path.exists(save_path) == False:
			os.makedirs(save_path)

		for i in range(0, len(targets["img_1_path"])):

			youtube_dir = save_path + targets["img_1_path"][i].split('/')[-2]

			if os.path.exists(youtube_dir) == False:
				os.makedirs(youtube_dir)

			saved_img = np.transpose(input_imgs[i,:,:,:].cpu().numpy(), (1,2,0))

			pred_d_ref = pred_d.data[i,:,:].cpu().numpy()
			pred_confidence_ref = pred_confidence.data[i,:,:].cpu().numpy()
			human_mask_ref = human_mask.data[i, 0, :,:].cpu().numpy()

			mvs_depth_ref = mvs_depth.data[i, :, :].cpu().numpy()

			input_depth_ref = input_depth[i,:,:].data.cpu().numpy()
			input_confidence_ref = input_confidence[i,0, :,:].data.cpu().numpy()
			full_flow_ref = full_flow[i, :, :, :].data.cpu().numpy()

			output_path = youtube_dir + '/' + targets["img_1_path"][i].split('/')[-1] 

			K = targets['K'][i]
			T_1_G = targets['T_1_G'][i]
			original_mvs_depth = targets['original_mvs_depth'][i]

			print('output_path', output_path)
			hdf5_file_write = h5py.File(output_path,'w')
			hdf5_file_write.create_dataset("/prediction/img", data = saved_img)            
			hdf5_file_write.create_dataset("/prediction/pred_depth", data=pred_d_ref)
			hdf5_file_write.create_dataset("/prediction/K", data=K)    
			hdf5_file_write.create_dataset("/prediction/T_1_G", data=T_1_G)    
			hdf5_file_write.create_dataset("/prediction/confidence", data=pred_confidence_ref)    
			hdf5_file_write.create_dataset("/prediction/human_mask", data=human_mask_ref)    
			hdf5_file_write.create_dataset("/prediction/input_depth", data=input_depth_ref)    
			hdf5_file_write.create_dataset("/prediction/input_confidence", data=input_confidence_ref)    
			hdf5_file_write.create_dataset("/prediction/mvs_depth", data=mvs_depth_ref)    
			hdf5_file_write.create_dataset("/prediction/full_flow", data=full_flow_ref)    
			hdf5_file_write.create_dataset("/prediction/original_mvs_depth", data=original_mvs_depth)    

			hdf5_file_write.close()



	def run_and_save_DAVIS(self, input_, targets, save_path):
		assert(self.num_input == 3)
		input_imgs = Variable(input_.cuda() , requires_grad = False)

		stack_inputs = input_imgs

		prediction_d, pred_confidence = self.netG.forward(stack_inputs) 
		pred_log_d = prediction_d.squeeze(1)
		pred_d = torch.exp(pred_log_d)

		if os.path.exists(save_path) == False:
			os.makedirs(save_path)

		for i in range(0, len(targets["img_1_path"])):

			youtube_dir = save_path + targets["img_1_path"][i].split('/')[-2]

			if os.path.exists(youtube_dir) == False:
				os.makedirs(youtube_dir)

			saved_img = np.transpose(input_imgs[i,:,:,:].cpu().numpy(), (1,2,0))

			pred_d_ref = pred_d.data[i,:,:].cpu().numpy()

			output_path = youtube_dir + '/' + targets["img_1_path"][i].split('/')[-1]
			print(output_path)
			disparity = 1./pred_d_ref
			disparity = disparity/np.max(disparity)
			disparity = np.tile(np.expand_dims(disparity, axis=-1), (1,1,3))
			saved_imgs = np.concatenate((saved_img, disparity), axis=1)

			imsave(output_path, saved_imgs)


	def switch_to_train(self):
		self.netG.train()

	def switch_to_eval(self):
		self.netG.eval()

	def save(self, label):
		self.save_network(self.netG, 'G', label, self.gpu_ids)
		# self.save_network(self.net_, 'G', label, self.gpu_ids)

	def update_learning_rate(self):

		self.scheduler.step()
		# for scheduler in self.schedulers:
			# scheduler.step()
		lr = self.optimizer_G.param_groups[0]['lr']
		print('Current learning rate = %.7f' % lr)





