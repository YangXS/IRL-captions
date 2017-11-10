import layer_utils
import matplotlib.pyplot as plt
import numpy as np
import tensorflow as tf

from image_utils import image_from_url
from discriminator.discriminator import CaptionInput, ImageInput, MetadataInput, LstmScalarRewardStrategy, Discriminator
from discriminator.discriminator_data_utils import create_demo_sampled_batcher


class DiscriminatorWrapper(object):
    def __init__(self, train_data, val_data, vocab_data):

        # Build graph
        caption_input = CaptionInput(word_embedding_init=vocab_data.embedding(), null_id=vocab_data.NULL_ID)
        image_input = ImageInput(image_feature_dim=train_data.image_feature_dim)
        metadata_input = MetadataInput()
        reward_config = LstmScalarRewardStrategy.RewardConfig(
            reward_scalar_transformer=lambda x: tf.nn.sigmoid(layer_utils.affine_transform(x, 1, 'hidden_to_reward'))
        )
        self.discr = Discriminator(caption_input, image_input, metadata_input, reward_config=reward_config,
                                   hidden_dim=512)

        self.train_data = train_data
        self.val_data = val_data
        self.vocab_data = vocab_data
        self.demo_batcher, self.sampled_batcher = create_demo_sampled_batcher(self.train_data)

        self.val_demo_batcher, self.val_sample_batcher = create_demo_sampled_batcher(self.val_data)

    def pre_train(self, sess, iter_num=400, batch_size=1000):

        return self.train(sess,
                          self.demo_batcher,
                          self.sampled_batcher, iter_num, batch_size)

    def train(self, sess, demo_batcher, sampled_batcher, iter_num, batch_size):
        train_losses = []
        val_losses = []
        for i in range(iter_num):

            image_idx_batch, caption_batch, demo_or_sampled_batch = self.process_mini_batch(
                demo_batcher,
                sampled_batcher,
                batch_size
            )

            loss, m, me = self._train_one_iter(sess, image_idx_batch, caption_batch, demo_or_sampled_batch)

            train_losses.append(loss)
            if i % 20 == 0:
                print("iter {}, loss: {}".format(i, loss))

            if i % 5 == 0:
                val_loss = self.examine_validation(sess, batch_size, to_examine=False)
                val_losses.append(val_loss)
            else:
                val_losses.append(val_losses[-1])
        return train_losses, val_losses

    def _train_one_iter(self, sess, image_idx_batch, caption_batch, demo_or_sampled_batch):
        caption_batch = caption_batch[:, 1::]
        image_feats_batch = self.train_data.get_image_features(image_idx_batch)
        self.discr.caption_input.pre_feed(caption_word_ids=caption_batch)
        self.discr.image_input.pre_feed(image_features=image_feats_batch)
        self.discr.metadata_input.pre_feed(demo_or_sampled_batch)
        loss, m, me = self.discr.train(sess)
        return loss, m, me

    def _preprocess_online_train_caption(self, caption):
        tokenized = [self.vocab_data.NULL_TOKEN] * self.train_data.max_caption_len
        tokenized[0] = self.vocab_data.START_TOKEN
        for i, tk in enumerate(caption.split()):
            pos = i + 1
            if pos >= self.train_data.max_caption_len:
                break
            tokenized[pos] = tk
        return tokenized

    def online_train(self, sess, iter_num, img_idxs, caption_sentences):

        captions = [self._preprocess_online_train_caption(c) for c in caption_sentences]
        caption_word_idx = self.vocab_data.encode_captions(captions)
        given_size = len(captions)

        new_dat = (img_idxs, caption_word_idx, np.zeros(given_size))
        mixed_sampled_batcher = self.demo_batcher.mix_new_data(new_dat, mix_ratio=1)

        batch_size = given_size

        return self.train(sess, self.demo_batcher, mixed_sampled_batcher, iter_num, batch_size)

    @staticmethod
    def process_mini_batch(batcher1, batcher2, batch_size):
        image_idx_batch1, caption_batch1, demo_or_sampled_batch1 = batcher1.sample(batch_size)
        image_idx_batch2, caption_batch2, demo_or_sampled_batch2 = batcher2.sample(batch_size)
        image_idx_batch = np.concatenate([image_idx_batch1, image_idx_batch2], axis=0)
        caption_batch = np.concatenate([caption_batch1, caption_batch2], axis=0)
        demo_or_sampled_batch = np.concatenate([demo_or_sampled_batch1, demo_or_sampled_batch2], axis=0)
        return image_idx_batch, caption_batch, demo_or_sampled_batch

    def assign_reward(self, sess, img_idxs, caption_sentences, image_idx_from_training=True, to_examine=True):
        captions = [c.split() for c in caption_sentences]

        if image_idx_from_training:
            coco_data = self.train_data
        else:
            coco_data = self.val_data
        image_feats_test = coco_data.get_image_features(img_idxs)
        caption_test = self.vocab_data.encode_captions(captions)
        loss, reward_per_token, mean_reward = self.run_test(sess, image_feats_test, caption_test)
        if to_examine:
            self.examine(coco_data, img_idxs, caption_test, reward_per_token, mean_reward)
        return loss, reward_per_token, mean_reward

    def run_validation(self, sess, img_idxs, caption_word_idx, demo_or_sampled_batch):
        image_feats = self.val_data.get_image_features(img_idxs)
        self.discr.image_input.pre_feed(image_feats)
        self.discr.caption_input.pre_feed(caption_word_idx)
        self.discr.metadata_input.pre_feed(labels=demo_or_sampled_batch)
        loss, reward_per_token, mean_reward = self.discr.test(sess)
        return loss, reward_per_token, mean_reward

    def run_test(self, sess, img_feature_test, caption_test):
        self.discr.image_input.pre_feed(img_feature_test)
        self.discr.caption_input.pre_feed(caption_test)
        self.discr.metadata_input.pre_feed(labels=np.ones(img_feature_test.shape[0]))
        loss, reward_per_token, mean_reward = self.discr.test(sess)
        return loss, reward_per_token, mean_reward

    def examine(self, coco_data, chosen_img, chosen_caption, chosen_reward_per_token, chosen_mean_reward):
        for (img_idx, cap, r, me_r) in zip(chosen_img, chosen_caption, chosen_reward_per_token, chosen_mean_reward):
            print("Avg reward: ", me_r)
            self.show_image_by_image_idxs(coco_data, [img_idx])
            decoded = self.vocab_data.decode_captions(cap).split()
            for (i, j) in zip(decoded, r):
                print("{:<15} {}".format(i, j))
            print("- - - -")

    def show_image_by_image_idxs(self, coco_data, img_idxs):
        """
            data indices to find image
        """
        urls = coco_data.get_urls_by_image_index(img_idxs)
        for url in urls:
            plt.imshow(image_from_url(url))
            plt.axis('off')
            plt.show()

    def examine_batch_results(self, demo_or_sampled_batch, image_id, caption_batch, reward_per_token,
                              mean_reward):
        num_to_examine = 4

        def examine_sample(chosen):
            print(chosen)
            chosen_img = image_id[chosen]
            chosen_cap = caption_batch[chosen]
            chosen_reward_per_token = reward_per_token[chosen]
            chosen_mean_reward = mean_reward[chosen]

            self.examine(chosen_img, chosen_cap, chosen_reward_per_token, chosen_mean_reward)

        print("DEMO RESULTS")
        chosen = np.random.choice(np.where(demo_or_sampled_batch == 1)[0], num_to_examine)
        examine_sample(chosen)
        print("\n\nSAMPLED RESULTS")
        chosen = np.random.choice(np.where(demo_or_sampled_batch == 0)[0], num_to_examine)
        examine_sample(chosen)

    def examine_validation(self, sess, batch_size=100, to_examine=True):
        image_idx_batch, caption_batch, demo_or_sampled_batch = self.process_mini_batch(self.val_demo_batcher,
                                                                                        self.val_sample_batcher,
                                                                                        batch_size)
        caption_batch = caption_batch[:, 1:]
        loss, reward_per_token, mean_reward = self.run_validation(sess, image_idx_batch, caption_batch,
                                                                  demo_or_sampled_batch)
        if to_examine:
            self.examine(self.val_data, image_idx_batch, caption_batch, reward_per_token, mean_reward)
        return loss